"""End of the free trial and the paid plan.

After the trial a business without an active subscription is softly blocked: it can still
see summaries and its recent entries, fix or undo entries, ask for its data or delete its
account, but new entries are not recorded until it subscribes. Nothing is ever charged here:
"ASSINAR" records the request and, when a payment provider is configured (Stripe in test
mode, see billing.py), answers with its checkout link. Access is granted only by a signed
provider event or by an operator.
"""

import logging
import unicodedata
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucromei.config import Settings, get_business_timezone
from lucromei.models import Business, Subscription
from lucromei.money import format_brl

logger = logging.getLogger(__name__)

REQUESTED = "requested"
PENDING_PAYMENT = "pending_payment"
ACTIVE = "active"
PAST_DUE = "past_due"
CANCELED = "canceled"
EXPIRED = "expired"
OPEN_STATUSES = (REQUESTED, PENDING_PAYMENT, ACTIVE, PAST_DUE)

SUBSCRIBE_COMMANDS = {
    "assinar",
    "quero assinar",
    "assinatura",
    "minha assinatura",
    "plano",
    "planos",
    "pagar",
    "pagamento",
    "quero pagar",
    "como assino",
    "como assinar",
    "continuar usando",
}


class CheckoutProvider(Protocol):
    def checkout_url(self, session: Session, business: Business, subscription: Subscription) -> str:
        """Create (or reuse) a hosted checkout for this subscription and return its URL."""
        ...


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split()).strip(" .!?")


def current_subscription(session: Session, business: Business) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .where(Subscription.business_id == business.id, Subscription.status.in_(OPEN_STATUSES))
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )


def is_paid(subscription: Subscription | None, now: datetime) -> bool:
    return (
        subscription is not None
        and subscription.status == ACTIVE
        and subscription.current_period_end is not None
        and _aware(subscription.current_period_end) > now
    )


def in_trial(business: Business, now: datetime) -> bool:
    return business.trial_ends_at is not None and _aware(business.trial_ends_at) > now


def has_access(session: Session, business: Business, now: datetime, settings: Settings) -> bool:
    """May this business record new entries?"""
    if not settings.trial_enforcement_enabled or in_trial(business, now):
        return True
    return is_paid(current_subscription(session, business), now)


def blocked_reply(business: Business, settings: Settings) -> str:
    ended = _local_date(business.trial_ends_at) if business.trial_ends_at else "recentemente"
    return (
        f"Seu teste grátis terminou em {ended}, por isso não anotei esta mensagem.\n\n"
        f"Para continuar anotando, mande ASSINAR. O plano custa "
        f"{format_brl(settings.subscription_price_cents)} por mês.\n\n"
        "Seus dados continuam guardados: você ainda pode pedir resumos, ver os últimos "
        "lançamentos, mandar MEUS DADOS ou APAGAR MEUS DADOS."
    )


def handle(
    session: Session,
    business: Business,
    *,
    text: str,
    now: datetime,
    settings: Settings,
    checkout: CheckoutProvider | None,
) -> str | None:
    """Answer ASSINAR and similar. Does not commit. None if the message is not for us."""

    if normalize(text) not in SUBSCRIBE_COMMANDS:
        return None
    subscription = current_subscription(session, business)
    if is_paid(subscription, now):
        return (
            "Sua assinatura está ativa até "
            f"{_local_date(subscription.current_period_end)}. Obrigado por usar o LucroMEI!"
        )
    if subscription is None:
        subscription = Subscription(
            business_id=business.id,
            status=REQUESTED,
            provider="none",
            price_cents=settings.subscription_price_cents,
            created_at=now,
            updated_at=now,
        )
        session.add(subscription)
        session.flush()
        logger.info("subscription_requested", extra={"business_id": business.id})
    price = format_brl(subscription.price_cents)
    trial = ""
    if in_trial(business, now):
        trial = f" Seu teste grátis continua até {_local_date(business.trial_ends_at)}."
    if checkout is None:
        return (
            f"Anotei seu interesse na assinatura do LucroMEI ({price} por mês).{trial}\n\n"
            "O pagamento pelo WhatsApp ainda não está disponível. Assim que estiver, eu te "
            "aviso por aqui. Nenhum valor será cobrado sem a sua confirmação."
        )
    url = checkout.checkout_url(session, business, subscription)
    return (
        f"Para assinar o LucroMEI ({price} por mês), pague neste link seguro:\n{url}\n\n"
        "Assim que o pagamento for confirmado, eu te aviso por aqui e você volta a anotar "
        f"normalmente.{trial}"
    )


def export_lines(session: Session, business: Business, now: datetime) -> list[str]:
    subscription = current_subscription(session, business)
    if subscription is None:
        return []
    if is_paid(subscription, now):
        return [f"Assinatura: ativa até {_local_date(subscription.current_period_end)}"]
    return [f"Assinatura: pedido feito em {_local_date(subscription.created_at)}, não ativa"]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _local_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return _aware(value).astimezone(get_business_timezone()).strftime("%d/%m/%Y")
