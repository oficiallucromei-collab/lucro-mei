import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from lucromei.config import is_configured_secret
from lucromei.money import format_brl


class InvalidWebhookSignature(ValueError):
    pass


class WhatsAppSendError(RuntimeError):
    """WhatsApp did not accept an outbound message.

    `code` is a short machine-readable reason, safe to log. `retryable` says whether sending the
    same message again later can succeed (timeouts, rate limits, Meta outages) or not (invalid
    number, expired credentials, closed 24-hour window).
    """

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WhatsAppInboundMessage:
    message_id: str
    sender: str
    message_type: str
    text: str | None
    sent_at: datetime | None


class OutboundSender(Protocol):
    def send_text(self, recipient: str, text: str) -> str | None:
        """Send a text message and return WhatsApp's ID for it, when known."""
        ...


@dataclass(slots=True)
class SentMessage:
    recipient: str
    text: str


@dataclass(slots=True)
class MockOutboundSender:
    """Records messages instead of sending them. Used in tests and local development."""

    sent_messages: list[SentMessage] = field(default_factory=list)

    def send_text(self, recipient: str, text: str) -> str:
        self.sent_messages.append(SentMessage(recipient=recipient, text=text))
        return f"mock.{len(self.sent_messages)}"


def verify_signature(body: bytes, signature_header: str | None, app_secret: str) -> None:
    """Validate Meta's X-Hub-Signature-256 without leaking timing information."""

    if not signature_header or not signature_header.startswith("sha256="):
        raise InvalidWebhookSignature("Assinatura do webhook ausente ou inválida.")
    if not is_configured_secret(app_secret):
        raise InvalidWebhookSignature("O segredo do aplicativo não está configurado.")
    expected = "sha256=" + hmac.new(app_secret.encode(), body, sha256).hexdigest()
    if not hmac.compare_digest(signature_header, expected):
        raise InvalidWebhookSignature("Assinatura do webhook inválida.")


def extract_inbound_messages(payload: dict[str, Any]) -> list[WhatsAppInboundMessage]:
    """Normalize the user messages in a webhook payload; delivery statuses are not included.

    Non-text messages (audio, images, stickers) are kept with `text=None` so the user can be
    told that only text is understood for now.
    """

    normalized: list[WhatsAppInboundMessage] = []
    for value in _change_values(payload):
        for message in value.get("messages") or []:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            sender = message.get("from")
            message_type = message.get("type")
            if not (_is_str(message_id) and _is_str(sender) and _is_str(message_type)):
                continue
            text = None
            if message_type == "text":
                body = (message.get("text") or {}).get("body")
                text = body if _is_str(body) and body.strip() else None
                if text is None:
                    continue
            normalized.append(
                WhatsAppInboundMessage(
                    message_id=message_id,
                    sender=sender,
                    message_type=message_type[:32],
                    text=text,
                    sent_at=_parse_timestamp(message.get("timestamp")),
                )
            )
    return normalized


def _change_values(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return []
    values = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if isinstance(change, dict) and change.get("field") == "messages":
                value = change.get("value")
                if isinstance(value, dict):
                    values.append(value)
    return values


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _parse_timestamp(raw: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def format_confirmation(transaction_type: str, amount_cents: int) -> str:
    amount = format_brl(amount_cents)
    labels = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}
    return f"{labels[transaction_type]} registrada: {amount}. Responda 'desfazer' para corrigir."


def build_sender(settings: Any) -> OutboundSender:
    """The sender used outside tests. Replaced by the real Cloud API sender when configured."""

    return MockOutboundSender()
