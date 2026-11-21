"""Ask instead of guessing: keep an ambiguous transaction pending until the user answers.

"Paguei 300 para o fornecedor" may be a business expense or a personal withdrawal. The amount
is stored as a pending entry and the user is asked; only the answer records the transaction.
Questions expire, so a late or stray "1" never records something the user has forgotten.
"""

import unicodedata
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucromei.domain import TransactionType
from lucromei.models import Business, PendingEntry
from lucromei.money import format_brl

OPEN = "open"
RESOLVED = "resolved"
CANCELLED = "cancelled"
EXPIRED = "expired"
REPLACED = "replaced"


class _Cancel:
    def __repr__(self) -> str:
        return "CANCEL"


CANCEL = _Cancel()

TYPE_LABELS = {
    TransactionType.INCOME: "entrada",
    TransactionType.EXPENSE: "despesa do negócio",
    TransactionType.WITHDRAWAL: "retirada pessoal",
}
_TYPE_ANSWERS = {
    TransactionType.INCOME: {"entrada", "e entrada", "recebimento", "venda"},
    TransactionType.EXPENSE: {
        "despesa",
        "e despesa",
        "despesa do negocio",
        "negocio",
        "do negocio",
        "e do negocio",
        "empresa",
        "da empresa",
        "e da empresa",
    },
    TransactionType.WITHDRAWAL: {
        "retirada",
        "e retirada",
        "retirada pessoal",
        "pessoal",
        "e pessoal",
        "pra mim",
        "para mim",
        "particular",
        "e particular",
    },
}
_CONFIRM_ANSWERS = {"sim", "s", "confirmo", "isso", "isso mesmo", "pode anotar", "certo", "ok"}
_CANCEL_ANSWERS = {
    "cancelar",
    "cancela",
    "esquece",
    "esquecer",
    "nao",
    "n",
    "nao anota",
    "nao anote",
    "nenhum",
    "nenhuma",
}

CANCELLED_REPLY = "Tudo bem, não anotei esse lançamento."
EXPIRED_REPLY = (
    "Essa pergunta já expirou e eu não anotei aquele lançamento. Se quiser, mande ele de novo."
)


def current_pending(
    session: Session, business: Business, now: datetime
) -> tuple[PendingEntry | None, PendingEntry | None]:
    """Return (open entry, expired entry). Expires the open entry lazily if its time is up.

    The expired entry is returned only when it is the latest question asked, so that a late
    answer to it can be told apart from an unrelated message.
    """

    latest = session.scalar(
        select(PendingEntry)
        .where(PendingEntry.business_id == business.id)
        .order_by(PendingEntry.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return None, None
    if latest.status == OPEN and _aware(latest.expires_at) <= now:
        close(latest, EXPIRED, now)
    if latest.status == OPEN:
        return latest, None
    if latest.status == EXPIRED:
        return None, latest
    return None, None


def create_pending(
    session: Session,
    business: Business,
    *,
    source_message_id: str,
    source_text: str,
    amount_cents: int,
    description: str,
    occurred_at: datetime,
    candidates: tuple[TransactionType, ...],
    origin: str,
    now: datetime,
    ttl: timedelta,
) -> PendingEntry:
    """Open a new question, replacing the open one if any. Does not commit."""

    for previous in session.scalars(
        select(PendingEntry).where(
            PendingEntry.business_id == business.id, PendingEntry.status == OPEN
        )
    ):
        close(previous, REPLACED, now)
    session.flush()  # the partial unique index allows a single open entry per business
    pending = PendingEntry(
        business_id=business.id,
        source_message_id=source_message_id,
        source_text=source_text,
        amount_cents=amount_cents,
        description=description[:500],
        occurred_at=occurred_at,
        candidate_types=[candidate.value for candidate in candidates],
        origin=origin,
        status=OPEN,
        created_at=now,
        expires_at=now + ttl,
    )
    session.add(pending)
    session.flush()
    return pending


def close(
    pending: PendingEntry,
    status: str,
    now: datetime,
    *,
    resolution_message_id: str | None = None,
    transaction_id: str | None = None,
) -> None:
    pending.status = status
    pending.closed_at = now
    pending.resolution_message_id = resolution_message_id
    pending.transaction_id = transaction_id


def candidates_of(pending: PendingEntry) -> tuple[TransactionType, ...]:
    return tuple(TransactionType(value) for value in pending.candidate_types)


def parse_answer(text: str, candidates: tuple[TransactionType, ...]):
    """Return the chosen TransactionType, CANCEL, or None when the text is not an answer."""

    answer = _normalize(text).strip(" .!")
    if answer in _CANCEL_ANSWERS:
        return CANCEL
    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1]
    if len(candidates) == 1 and answer in _CONFIRM_ANSWERS:
        return candidates[0]
    for candidate in candidates:
        if answer in _TYPE_ANSWERS[candidate]:
            return candidate
    return None


def looks_like_answer(text: str) -> bool:
    """Would this text have answered a question? Used to explain that a question expired."""

    answer = _normalize(text).strip(" .!")
    every_type = set().union(*_TYPE_ANSWERS.values())
    return answer in every_type or answer in _CONFIRM_ANSWERS or answer in {"1", "2", "3"}


def question(pending: PendingEntry) -> str:
    candidates = candidates_of(pending)
    amount = format_brl(pending.amount_cents)
    if len(candidates) == 1:
        label = TYPE_LABELS[candidates[0]]
        return (
            f"Entendi assim: {label} de {amount} ({pending.description}).\n\n"
            "Posso anotar? Responda SIM para anotar ou NÃO para cancelar."
        )
    options = " ou ".join(TYPE_LABELS[candidate] for candidate in candidates)
    numbered = " ou ".join(
        f"{index} para {TYPE_LABELS[candidate]}" for index, candidate in enumerate(candidates, 1)
    )
    return (
        f"Antes de anotar {amount} ({pending.description}), me diz: é {options}?\n\n"
        f"Responda {numbered}."
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split())
