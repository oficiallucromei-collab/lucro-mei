"""First-contact onboarding: name, type of business, terms acceptance, start of the free trial.

Nothing is written to the ledger before the terms are accepted. The conversation is a small
state machine stored on the business row; each step runs inside the message-processing
transaction, with the business row locked, so a step can never be applied twice.
"""

import re
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lucromei.config import Settings, get_business_timezone
from lucromei.domain import ParseError, parse_transaction
from lucromei.models import Business, TermsAcceptance

NEW = "new"
AWAITING_NAME = "awaiting_name"
AWAITING_BUSINESS_TYPE = "awaiting_business_type"
AWAITING_TERMS = "awaiting_terms"
ACTIVE = "active"

ACCEPT_ANSWERS = {
    "aceito",
    "aceito os termos",
    "eu aceito",
    "sim aceito",
    "sim eu aceito",
    "concordo",
    "eu concordo",
    "li e aceito",
}
DECLINE_ANSWERS = {"nao", "nao aceito", "nao concordo", "recuso"}
_NAME_PREFIXES = re.compile(
    r"^(?:(?:oi|ola|bom dia|boa tarde|boa noite)[,!.\s]+)?"
    r"(?:meu nome e|me chamo|eu sou o|eu sou a|eu sou|sou o|sou a|sou|aqui e o|aqui e a)\s+",
)
MAX_NAME_LENGTH = 60
MAX_BUSINESS_TYPE_LENGTH = 80

WELCOME = (
    "Olá! Eu sou o LucroMEI. Vou te ajudar a anotar entradas, despesas e retiradas do seu "
    "negócio aqui pelo WhatsApp.\n\nPara começar, como você se chama?"
)
NOT_RECORDED_YET = (
    "Ainda não anotei esse lançamento: primeiro preciso terminar seu cadastro. "
    "Depois é só mandar de novo."
)
ASK_NAME_AGAIN = "Não consegui entender seu nome. Como você gostaria que eu te chamasse?"
ASK_BUSINESS_TYPE_AGAIN = (
    "Me conta em poucas palavras qual é o seu negócio. Por exemplo: barbearia, salão de beleza, "
    "manicure, lanchonete."
)


class OnboardingRequired(ValueError):
    """The business has not finished onboarding (and accepted the terms)."""


def is_active(business: Business | None) -> bool:
    return business is not None and business.onboarding_step == ACTIVE


def handle(
    session: Session,
    business: Business,
    *,
    message_id: str,
    text: str,
    now: datetime,
    settings: Settings,
) -> str:
    """Advance onboarding with this message and return the reply. Does not commit."""

    step = business.onboarding_step
    if step == NEW:
        business.onboarding_step = AWAITING_NAME
        if _looks_like_transaction(text):
            return f"{WELCOME}\n\n{NOT_RECORDED_YET}"
        return WELCOME

    if step == AWAITING_NAME:
        name = extract_name(text)
        if name is None:
            return ASK_NAME_AGAIN
        business.display_name = name
        business.onboarding_step = AWAITING_BUSINESS_TYPE
        return (
            f"Prazer, {name.split()[0]}! Qual é o seu tipo de negócio? "
            "Por exemplo: barbearia, salão de beleza, manicure, lanchonete."
        )

    if step == AWAITING_BUSINESS_TYPE:
        business_type = extract_business_type(text)
        if business_type is None:
            return ASK_BUSINESS_TYPE_AGAIN
        business.business_type = business_type
        business.onboarding_step = AWAITING_TERMS
        return terms_message(settings)

    if step == AWAITING_TERMS:
        answer = _normalize(text).strip(" .!")
        if answer in ACCEPT_ANSWERS:
            return _accept_terms(session, business, message_id, text, now, settings)
        if answer in DECLINE_ANSWERS:
            return (
                "Tudo bem. Sem o aceite dos termos eu não consigo registrar seus lançamentos. "
                "Se mudar de ideia, é só responder ACEITO."
            )
        return "Para continuar, responda ACEITO. " + _terms_link_sentence(settings)

    raise ValueError(f"Etapa de cadastro desconhecida: {step}")


def terms_message(settings: Settings) -> str:
    return (
        "Último passo: para usar o LucroMEI você precisa aceitar os Termos de Uso e a "
        f"Política de Privacidade (versão {settings.terms_version}). "
        + _terms_link_sentence(settings)
        + "\n\nResumindo: eu guardo os lançamentos que você me mandar para calcular seus "
        "resumos. Não dou orientação contábil, fiscal nem de investimentos. Quando quiser, "
        "mande MEUS DADOS para ver tudo o que guardo ou APAGAR MEUS DADOS para apagar.\n\n"
        "Se você concorda, responda ACEITO."
    )


def _terms_link_sentence(settings: Settings) -> str:
    if settings.terms_url:
        return f"Você pode ler tudo aqui: {settings.terms_url}"
    return "Peça o texto completo se quiser ler antes."


def _accept_terms(
    session: Session,
    business: Business,
    message_id: str,
    text: str,
    now: datetime,
    settings: Settings,
) -> str:
    session.add(
        TermsAcceptance(
            business_id=business.id,
            terms_version=settings.terms_version,
            accepted_at=now,
            source_message_id=message_id,
            accepted_text=text.strip()[:100],
        )
    )
    business.onboarding_step = ACTIVE
    business.trial_started_at = now
    business.trial_ends_at = now + timedelta(days=settings.trial_days)
    trial_end = business.trial_ends_at.astimezone(get_business_timezone()).strftime("%d/%m/%Y")
    name = (business.display_name or "").split()[0] if business.display_name else ""
    greeting = f"Tudo certo, {name}!" if name else "Tudo certo!"
    return (
        f"{greeting} Seu teste grátis de {settings.trial_days} dias começou hoje e vai até "
        f"{trial_end}.\n\n"
        "Agora é só me mandar suas movimentações, uma por mensagem. Por exemplo:\n"
        "• Recebi 150 por um corte\n"
        "• Gastei 80 em produtos\n"
        "• Retirei 200 para mim\n\n"
        "Para ver como está o mês, mande: resumo do mês"
    )


def extract_name(text: str) -> str | None:
    # NFC so that accent folding keeps every character at the same index.
    cleaned = " ".join(unicodedata.normalize("NFC", text).strip().split())
    folded = _normalize(cleaned)
    if prefix := _NAME_PREFIXES.match(folded):
        cleaned = cleaned[prefix.end() :]
    cleaned = cleaned.strip(" .,!?;:")
    if not cleaned or any(char.isdigit() for char in cleaned):
        return None
    if not any(char.isalpha() for char in cleaned) or len(cleaned) > MAX_NAME_LENGTH:
        return None
    if len(cleaned.split()) > 5:
        return None
    return " ".join(word[:1].upper() + word[1:] for word in cleaned.split())


def extract_business_type(text: str) -> str | None:
    cleaned = " ".join(text.strip().split()).strip(" .,!?;:")
    if len(cleaned) < 2 or not any(char.isalpha() for char in cleaned):
        return None
    return cleaned[:MAX_BUSINESS_TYPE_LENGTH]


def _looks_like_transaction(text: str) -> bool:
    try:
        parse_transaction(text)
    except ParseError:
        return False
    return True


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split())
