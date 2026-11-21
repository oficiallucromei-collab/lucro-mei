"""Proactive notices: the end of the free trial and, optionally, reminders after inactivity.

The hourly maintenance job asks for the notices due now; each one is queued once, keyed
"notice:<business>:<kind>" in outbound_messages.in_reply_to (unique), so a notice is never
sent twice however many workers run maintenance. Within 24 hours of the user's last message
the notice goes out as plain text; later it uses the approved Meta template (templates.py).

Everything here is off unless LUCROMEI_NOTIFICATIONS_ENABLED is true, and reminders also need
LUCROMEI_REMINDERS_ENABLED. Notices are only sent between the configured local hours, never to
accounts waiting to be erased, and reminders respect PARAR LEMBRETES.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucromei import subscription
from lucromei.config import Settings, get_business_timezone
from lucromei.models import Business, DeletionRequest, InboundEvent, Transaction
from lucromei.templates import REMINDER, TRIAL_ENDED, TRIAL_ENDING, Template

TRIAL_ENDING_NOTICE = timedelta(hours=48)
TRIAL_ENDED_NOTICE = timedelta(days=3)
# Free-form text is accepted for 24 hours after the user's last message; keep a margin.
SERVICE_WINDOW = timedelta(hours=23)

OPT_OUT_COMMANDS = {
    "parar lembretes",
    "parar lembrete",
    "sem lembretes",
    "nao quero lembretes",
    "cancelar lembretes",
    "desativar lembretes",
    "parar",
}
OPT_IN_COMMANDS = {"voltar lembretes", "quero lembretes", "ativar lembretes", "ligar lembretes"}


@dataclass(frozen=True, slots=True)
class Notice:
    business_id: str
    recipient: str
    key: str
    template: Template
    parameters: list[str]
    use_template: bool

    @property
    def text(self) -> str:
        return self.template.render(self.parameters)


def due_notices(session: Session, now: datetime, settings: Settings) -> list[Notice]:
    if not settings.notifications_enabled:
        return []
    tz = get_business_timezone()
    hour = now.astimezone(tz).hour
    if not settings.notification_start_hour <= hour < settings.notification_end_hour:
        return []
    erasing = select(DeletionRequest.business_id).where(
        DeletionRequest.status.in_(["awaiting_confirmation", "scheduled"])
    )
    businesses = session.scalars(
        select(Business).where(
            Business.onboarding_step == "active",
            Business.trial_ends_at.is_not(None),
            Business.id.not_in(erasing),
        )
    ).all()
    notices: list[Notice] = []
    for business in businesses:
        notices += _notices_for(session, business, now, settings)
    return notices


def _notices_for(
    session: Session, business: Business, now: datetime, settings: Settings
) -> list[Notice]:
    name = (business.display_name or "").split()[0] if business.display_name else "tudo bem"
    trial_end = _aware(business.trial_ends_at)
    paid = subscription.is_paid(subscription.current_subscription(session, business), now)
    window_open = _last_message_at(session, business) >= now - SERVICE_WINDOW
    notices = []

    def notice(key: str, template: Template, parameters: list[str]) -> Notice:
        return Notice(
            business_id=business.id,
            recipient=business.whatsapp_number,
            key=f"notice:{business.id}:{key}",
            template=template,
            parameters=parameters,
            use_template=not window_open,
        )

    if settings.trial_enforcement_enabled and not paid:
        end_label = _local_date(trial_end)
        if timedelta(0) < trial_end - now <= TRIAL_ENDING_NOTICE:
            notices.append(notice("trial_ending", TRIAL_ENDING, [name, end_label]))
        elif timedelta(0) <= now - trial_end < TRIAL_ENDED_NOTICE:
            notices.append(notice("trial_ended", TRIAL_ENDED, [name, end_label]))

    if (
        settings.reminders_enabled
        and business.reminders_opt_out_at is None
        and subscription.has_access(session, business, now, settings)
    ):
        last_entry = session.scalar(
            select(func.max(Transaction.created_at)).where(
                Transaction.business_id == business.id
            )
        )
        since = _aware(last_entry or business.trial_started_at or now)
        idle_days = (now - since).days
        if idle_days >= settings.reminder_inactive_days:
            # One reminder per stretch of inactivity, keyed by when it started.
            key = f"reminder:{since.date().isoformat()}"
            notices.append(notice(key, REMINDER, [name, str(idle_days)]))
    return notices


def needs_template(session: Session, business: Business, now: datetime) -> bool:
    """Is the 24-hour window closed, so only an approved template will be delivered?"""
    return _last_message_at(session, business) < now - SERVICE_WINDOW


def _last_message_at(session: Session, business: Business) -> datetime:
    last = session.scalar(
        select(func.max(InboundEvent.sent_at)).where(
            InboundEvent.sender == business.whatsapp_number
        )
    )
    return _aware(last) if last else datetime.min.replace(tzinfo=UTC)


def handle_opt_out(business: Business, normalized: str, now: datetime) -> str | None:
    """PARAR LEMBRETES / VOLTAR LEMBRETES. Does not commit."""
    command = normalized.strip(" .!?")
    if command in OPT_OUT_COMMANDS:
        business.reminders_opt_out_at = business.reminders_opt_out_at or now
        return (
            "Pronto, não vou mais mandar lembretes. Avisos sobre a sua conta, como o fim do "
            "teste grátis, continuam. Para voltar, mande VOLTAR LEMBRETES."
        )
    if command in OPT_IN_COMMANDS:
        business.reminders_opt_out_at = None
        return "Combinado, os lembretes estão ligados de novo."
    return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _local_date(value: datetime) -> str:
    return _aware(value).astimezone(get_business_timezone()).strftime("%d/%m/%Y")
