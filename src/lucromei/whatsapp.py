import hmac
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol

import httpx


class WhatsAppSendError(RuntimeError):
    """WhatsApp could not accept an outbound message."""


class InvalidWebhookSignature(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WhatsAppTextMessage:
    message_id: str
    sender: str
    text: str


class OutboundSender(Protocol):
    def send_text(self, recipient: str, text: str) -> None: ...


@dataclass(slots=True)
class SentMessage:
    recipient: str
    text: str


@dataclass(slots=True)
class MockOutboundSender:
    sent_messages: list[SentMessage] = field(default_factory=list)

    def send_text(self, recipient: str, text: str) -> None:
        self.sent_messages.append(SentMessage(recipient=recipient, text=text))


@dataclass(slots=True)
class CloudApiOutboundSender:
    access_token: str = field(repr=False)
    phone_number_id: str = field(repr=False)
    graph_api_version: str = "v25.0"

    def send_text(self, recipient: str, text: str) -> None:
        url = f"https://graph.facebook.com/{self.graph_api_version}/{self.phone_number_id}/messages"
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"body": text},
                },
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WhatsAppSendError(
                f"WhatsApp rejected the outbound message (HTTP {exc.response.status_code})."
            ) from None
        except httpx.RequestError:
            raise WhatsAppSendError("Could not reach the WhatsApp Cloud API.") from None


def verify_signature(body: bytes, signature_header: str | None, app_secret: str) -> None:
    """Validate Meta's X-Hub-Signature-256 without leaking timing information."""

    if not signature_header or not signature_header.startswith("sha256="):
        raise InvalidWebhookSignature("Assinatura do webhook ausente ou inválida.")
    if not app_secret or app_secret == "change-me":
        raise InvalidWebhookSignature("O segredo do aplicativo não está configurado.")
    expected = "sha256=" + hmac.new(app_secret.encode(), body, sha256).hexdigest()
    if not hmac.compare_digest(signature_header, expected):
        raise InvalidWebhookSignature("Assinatura do webhook inválida.")


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def extract_text_messages(
    payload: Any, phone_number_id: str | None = None
) -> list[WhatsAppTextMessage]:
    """Normalize supported text messages for the configured WhatsApp number."""

    normalized: list[WhatsAppTextMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return normalized
    for entry in _object_list(payload.get("entry")):
        for change in _object_list(entry.get("changes")):
            value = change.get("value")
            if change.get("field") != "messages" or not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if phone_number_id is not None and (
                not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id
            ):
                continue
            for message in _object_list(value.get("messages")):
                text_data = message.get("text")
                if message.get("type") != "text" or not isinstance(text_data, dict):
                    continue
                text = text_data.get("body")
                message_id = message.get("id")
                sender = message.get("from")
                if (
                    isinstance(text, str)
                    and 0 < len(text) <= 4096
                    and isinstance(message_id, str)
                    and 0 < len(message_id) <= 255
                    and isinstance(sender, str)
                    and re.fullmatch(r"[+]?[0-9]{1,32}", sender)
                ):
                    normalized.append(WhatsAppTextMessage(message_id, sender, text))
    return normalized


def format_confirmation(transaction_type: str, amount_cents: int, duplicate: bool) -> str:
    amount = f"R$ {amount_cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if duplicate:
        return f"Essa mensagem já foi registrada: {amount}."
    labels = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}
    return f"{labels[transaction_type]} registrada: {amount}. Responda 'desfazer' para corrigir."
