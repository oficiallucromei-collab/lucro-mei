"""Acquisition and entitlement state shared by channel adapters."""

import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from lucromei.conversation import ConversationService, _money
from lucromei.domain import ParseError
from lucromei.models import (
    Acquisition,
    AcquisitionClick,
    Business,
    CommandReceipt,
    GrowthEvent,
    InboundMessage,
    Transaction,
    TransactionEvent,
    TrialHold,
    new_id,
)
from lucromei.service import (
    LedgerService,
    MessageOwnerMismatch,
    normalize_whatsapp_number,
)
from lucromei.whatsapp import format_confirmation

BRAZIL = ZoneInfo("America/Sao_Paulo")
TOKEN = re.compile(r"^Quero experimentar LucroMEI ([A-Za-z0-9_-]{32})$", re.IGNORECASE)
TAG = re.compile(r"[A-Za-z0-9_.-]{1,80}")
SEGMENTS = dict(enumerate(("beleza", "alimentacao", "servicos", "comercio", "outro"), start=1))
PROMPT = "Envie uma operação, por exemplo: Recebi R$150 por um serviço."
SECTORS = "Com o que você trabalha? 1 Beleza · 2 Alimentação · 3 Serviços · 4 Comércio · 5 Outro."


def insert_for(session, model):
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    return insert(model)


def utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def record(session, business_id, name, now, amount=None):
    result = session.execute(
        insert_for(session, GrowthEvent)
        .values(business_id=business_id, name=name, happened_at=now, amount_cents=amount)
        .on_conflict_do_nothing(index_elements=["business_id", "name"])
    )
    return result.rowcount == 1


def trial_identity(number, secret):
    return hmac.new(secret.encode(), number.encode(), sha256).hexdigest()


def access(profile, now):
    if profile.subscription_status == "active" and profile.paid_through:
        if utc(profile.paid_through) > now:
            return "paid"
    if profile.trial_end is None:
        return "legacy"
    if utc(profile.trial_end) > now:
        return "trial"
    return "expired"


def claim_attribution(session, text, referral_source, now):
    if match := TOKEN.fullmatch(text):
        attribution = session.execute(
            update(AcquisitionClick)
            .where(
                AcquisitionClick.token == match[1],
                AcquisitionClick.created_at >= now - timedelta(hours=24),
                AcquisitionClick.claimed.is_(False),
            )
            .values(claimed=True)
            .returning(AcquisitionClick.attribution)
        ).scalar_one_or_none()
        if attribution is not None:
            return attribution
    if referral_source:
        return {
            "utm_source": "meta",
            "utm_medium": "click_to_whatsapp",
            "meta_source_id": referral_source,
        }
    return {"utm_source": "direct"}


def handle_growth_message(session, settings, message, *, now=None):
    now = now or datetime.now(UTC)
    number = normalize_whatsapp_number(message.sender)
    text = message.text.strip()
    command = text.casefold()
    new = (
        session.execute(
            insert_for(session, Business)
            .values(id=new_id(), whatsapp_number=number, created_at=now)
            .on_conflict_do_nothing(index_elements=["whatsapp_number"])
        ).rowcount
        == 1
    )
    business_id = session.scalar(
        select(Business.id).where(Business.whatsapp_number == number).with_for_update()
    )
    receipt = session.scalar(
        select(CommandReceipt).where(CommandReceipt.external_id == message.message_id)
    )
    if receipt:
        if receipt.business_id != business_id:
            raise MessageOwnerMismatch("Message identifier belongs to a different sender.")
        return receipt.response_text, True
    inbound = session.scalar(
        select(InboundMessage).where(InboundMessage.external_id == message.message_id)
    )
    if inbound:
        if inbound.business_id != business_id:
            raise MessageOwnerMismatch("Message identifier belongs to a different sender.")
        return None, True
    profile = session.get(Acquisition, business_id)
    if profile is None:
        hold = (
            session.get(TrialHold, trial_identity(number, settings.growth_trial_hold_key))
            if new and len(settings.growth_trial_hold_key) >= 32
            else None
        )
        trial_allowed = new and not (hold and utc(hold.expires_at) > now)
        profile = Acquisition(
            business_id=business_id,
            attribution=claim_attribution(session, text, message.referral_source, now)
            if new
            else {},
            trial_start=now if trial_allowed else None,
            trial_end=(
                now + timedelta(days=settings.growth_trial_days)
                if trial_allowed
                else now
                if new
                else None
            ),
            activation_count=settings.growth_activation_count,
            activation_hours=settings.growth_activation_hours,
            last_inbound=now,
        )
        session.add(profile)
        if trial_allowed:
            for name in ("conversation_started", "onboarding_started", "trial_started"):
                record(session, business_id, name, now)

    if profile.trial_start:
        elapsed = now - utc(profile.trial_start)
        for day in (7, 14, 30):
            if timedelta(days=day) <= elapsed < timedelta(days=day + 1):
                record(session, business_id, f"returned_day_{day}", now)
    profile.last_inbound = max(utc(profile.last_inbound), now)

    if command in {"parar", "sair", "stop"}:
        profile.opt_in_at = None
        record(session, business_id, "opted_out", now)
        reply = "Lembretes desativados. Você pode continuar usando o LucroMEI."
    elif command == "aceito lembretes":
        profile.opt_in_at = now
        record(session, business_id, "opted_in", now)
        reply = (
            "Você aceitou lembretes da avaliação e avisos semanais. "
            "Envie PARAR quando quiser desativá-los."
        )
    elif command in {"exportar meus dados", "excluir minha conta"}:
        kind = "export" if command.startswith("exportar") else "deletion"
        record(session, business_id, f"requested_{kind}:{message.message_id[-48:]}", now)
        reply = "Recebemos seu pedido de privacidade e vamos analisá-lo."
    elif command in {"assinar", "gerenciar assinatura"}:
        from lucromei.billing import billing_reply

        reply = billing_reply(
            profile, settings, now, manage=command != "assinar", message_id=message.message_id
        )
    elif command == "indicar" and settings.growth_referrals_enabled:
        if access(profile, now) != "trial" or not settings.growth_public_url:
            reply = "O programa de indicação está disponível durante a avaliação."
        else:
            if not profile.referral_code:
                profile.referral_code = secrets.token_urlsafe(18)
            query = urlencode({"referral_id": profile.referral_code, "utm_source": "referral"})
            reply = f"Compartilhe seu link de indicação: {settings.growth_public_url}/?{query}"
    elif command == "meu plano":
        remaining = (
            max(
                0,
                (utc(profile.trial_end) - now + timedelta(days=1) - timedelta(microseconds=1)).days,
            )
            if profile.trial_end
            else 0
        )
        labels = {
            "paid": "Assinatura ativa",
            "legacy": "Acesso de pré-lançamento",
            "trial": f"Avaliação ativa. Dias restantes: {remaining}",
            "expired": "Avaliação encerrada. Envie ASSINAR para ver as opções.",
        }
        reply = labels[access(profile, now)]
    elif settings.growth_enforce_trial and access(profile, now) == "expired":
        reply = (
            "Sua avaliação terminou. Envie ASSINAR para continuar ou "
            "EXPORTAR MEUS DADOS para solicitar seus dados."
        )
    elif new and profile.trial_start:
        reply = (
            f"Olá! Sou o LucroMEI. Controle entradas, despesas e retiradas pelo WhatsApp. "
            f"Você tem {settings.growth_trial_days} dias de avaliação gratuita. {SECTORS} "
            f"Se preferir, comece direto. {PROMPT}"
        )
        # A valid first transaction can also start the conversation without friction.
        if transaction_reply := save_transaction(session, profile, message, now, settings):
            reply += "\n\n" + transaction_reply
    elif profile.trial_start and profile.segment is None and re.fullmatch("[1-5]", command):
        profile.segment = SEGMENTS[int(command)]
        reply = PROMPT
    elif result := ConversationService(session).handle(
        message_id=message.message_id, whatsapp_number=number, text=text, now=now, commit=False
    ):
        if result.command_type == "summary":
            record(session, business_id, "summary_requested", now)
        return result.reply, result.duplicate
    else:
        reply = save_transaction(session, profile, message, now, settings)
        if reply is None:
            reply = f"{PROMPT} Para consultar seu saldo, envie RESUMO DO MÊS."
            if profile.trial_start and profile.segment is None:
                reply = f"{SECTORS} {reply}"

    session.add(
        CommandReceipt(
            external_id=message.message_id,
            business_id=business_id,
            command_type="growth",
            response_text=reply,
        )
    )
    return reply, False


def save_transaction(session, profile, message, now, settings):
    ledger = LedgerService(session)
    try:
        result = ledger.process(
            message_id=message.message_id,
            whatsapp_number=message.sender,
            text=message.text,
            commit=False,
        )
    except ParseError:
        return None
    entry = result.transaction
    reply = format_confirmation(entry.transaction_type.value, entry.amount_cents, result.duplicate)
    if not profile.trial_start or result.duplicate:
        return reply
    owner = profile.business_id
    if record(session, owner, "first_transaction", now):
        record(session, owner, "onboarding_completed", now)
        local = now.astimezone(BRAZIL)
        summary = ledger.monthly_summary(message.sender, local.year, local.month)
        reply += (
            f"\n\nNeste mês: entradas {_money(summary.income_cents)}, despesas "
            f"{_money(summary.expense_cents)}, retiradas {_money(summary.withdrawal_cents)}. "
            f"Saldo {_money(summary.operating_balance_cents)}. "
            "Para receber lembretes da avaliação e avisos semanais, envie ACEITO LEMBRETES. "
            "Para sair, envie PARAR."
        )
        record(session, owner, "summary_prepared", now)
    pending_third = session.get(GrowthEvent, (owner, "third_transaction")) is None
    pending_activation = (
        now <= utc(profile.trial_start) + timedelta(hours=profile.activation_hours)
        and now < utc(profile.trial_end)
        and session.get(GrowthEvent, (owner, "activated")) is None
    )
    if not (pending_third or pending_activation):
        return reply
    reversed_transaction = (
        select(TransactionEvent.id)
        .where(
            TransactionEvent.transaction_id == Transaction.id,
            TransactionEvent.event_type == "reversed",
        )
        .exists()
    )
    count = len(
        session.scalars(
            select(Transaction.id)
            .where(
                Transaction.business_id == owner,
                Transaction.created_at >= profile.trial_start,
                ~reversed_transaction,
            )
            .limit(max(3, profile.activation_count))
        ).all()
    )
    if pending_third and count >= 3:
        record(session, owner, "third_transaction", now)
    if pending_activation and count >= profile.activation_count:
        if record(session, owner, "activated", now):
            if settings.growth_referrals_enabled and award_referral(
                session, profile, now, settings
            ):
                reply += f"\nSua indicação rendeu mais {settings.growth_referral_reward_days} dias."
            reply += "\n\nVocê já pode acompanhar seu saldo com RESUMO DO MÊS."
    return reply


def claim_reply(session, message, now):
    business_id = session.scalar(
        select(Business.id).where(
            Business.whatsapp_number == normalize_whatsapp_number(message.sender)
        )
    )
    return business_id is not None and record(
        session, business_id, f"reply:{sha256(message.message_id.encode()).hexdigest()}", now
    )


def award_referral(session, invited, now, settings):
    code = invited.attribution.get("referral_id")
    if not code or not re.fullmatch(r"[A-Za-z0-9_-]{24}", code):
        return False
    inviter_id = session.scalar(
        select(Acquisition.business_id).where(Acquisition.referral_code == code)
    )
    if not inviter_id or inviter_id == invited.business_id:
        return False
    session.execute(select(Business.id).where(Business.id == inviter_id).with_for_update())
    inviter = session.get(Acquisition, inviter_id, populate_existing=True)
    if access(inviter, now) != "trial" or access(invited, now) != "trial":
        return False
    if not record(session, inviter_id, "referral_reward_inviter", now):
        return False
    days = timedelta(days=settings.growth_referral_reward_days)
    inviter.trial_end = utc(inviter.trial_end) + days
    invited.trial_end = utc(invited.trial_end) + days
    record(session, invited.business_id, "referral_reward_invited", now)
    return True
