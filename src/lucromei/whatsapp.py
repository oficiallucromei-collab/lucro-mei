import hmac
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


def extract_text_messages(payload: dict[str, Any]) -> list[WhatsAppTextMessage]:
    """Normalize supported text messages and ignore delivery statuses or media."""

    normalized: list[WhatsAppTextMessage] = []
    if payload.get("object") != "whatsapp_business_account":
        return normalized
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            for message in change.get("value", {}).get("messages", []):
                text = message.get("text", {}).get("body")
                if message.get("type") != "text" or not text:
                    continue
                message_id = message.get("id")
                sender = message.get("from")
                if message_id and sender:
                    normalized.append(
                        WhatsAppTextMessage(
                            message_id=message_id,
                            sender=sender,
                            text=text,
                        )
                    )
    return normalized


def format_confirmation(transaction_type: str, amount_cents: int, duplicate: bool) -> str:
    amount = f"R$ {amount_cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if duplicate:
        return f"Essa mensagem já foi registrada: {amount}."
    labels = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}
    return f"{labels[transaction_type]} registrada: {amount}. Responda 'desfazer' para corrigir."
