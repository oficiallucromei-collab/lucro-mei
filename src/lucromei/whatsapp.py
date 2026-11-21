import hmac
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Any, Protocol

import httpx

from lucromei.config import is_configured_secret
from lucromei.money import format_brl

logger = logging.getLogger(__name__)
MEDIA_ID_RE = re.compile(r"[0-9]{1,64}")


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
    media_id: str | None = None
    phone_number_id: str | None = None


class OutboundSender(Protocol):
    def send_text(self, recipient: str, text: str) -> str | None:
        """Send a text message and return WhatsApp's ID for it, when known."""
        ...

    def send_template(
        self, recipient: str, name: str, language: str, parameters: list[str]
    ) -> str | None:
        """Send an approved message template (works outside the 24-hour window)."""
        ...

    def send_document(
        self, recipient: str, filename: str, content: bytes, mime_type: str, caption: str
    ) -> str | None:
        """Upload a file and send it as a document."""
        ...


@dataclass(slots=True)
class SentMessage:
    recipient: str
    text: str
    template: str | None = None
    parameters: list[str] | None = None
    document: bytes | None = None
    filename: str | None = None


@dataclass(slots=True)
class MockOutboundSender:
    """Records messages instead of sending them. Used in tests and local development."""

    sent_messages: list[SentMessage] = field(default_factory=list)

    def send_text(self, recipient: str, text: str) -> str:
        self.sent_messages.append(SentMessage(recipient=recipient, text=text))
        return f"mock.{len(self.sent_messages)}"

    def send_template(
        self, recipient: str, name: str, language: str, parameters: list[str]
    ) -> str:
        self.sent_messages.append(
            SentMessage(
                recipient=recipient,
                text=f"[template {name}] " + " | ".join(parameters),
                template=name,
                parameters=list(parameters),
            )
        )
        return f"mock.{len(self.sent_messages)}"

    def send_document(
        self, recipient: str, filename: str, content: bytes, mime_type: str, caption: str
    ) -> str:
        self.sent_messages.append(
            SentMessage(recipient=recipient, text=caption, document=content, filename=filename)
        )
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

    Audio messages carry only a validated media ID. Other media keep `text=None`.
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
            audio = message.get("audio")
            media_id = (
                audio.get("id") if message_type == "audio" and isinstance(audio, dict) else None
            )
            metadata = value.get("metadata")
            phone_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            normalized.append(
                WhatsAppInboundMessage(
                    message_id=message_id,
                    sender=sender,
                    message_type=message_type[:32],
                    text=text,
                    sent_at=_parse_timestamp(message.get("timestamp")),
                    media_id=media_id if isinstance(media_id, str) and MEDIA_ID_RE.fullmatch(
                        media_id
                    ) else None,
                    phone_number_id=phone_id if isinstance(phone_id, str) else None,
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


def format_confirmation(
    transaction_type: str, amount_cents: int, *, occurred_on: date | None = None
) -> str:
    amount = format_brl(amount_cents)
    labels = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}
    when = f" em {occurred_on.strftime('%d/%m/%Y')}" if occurred_on else ""
    return (
        f"{labels[transaction_type]} registrada{when}: {amount}. "
        "Responda 'desfazer' para corrigir."
    )


# Meta error codes worth retrying: throttling and temporary platform errors. Everything else in a
# 4xx response (invalid number, closed 24-hour window, bad token, bad parameter) will fail again.
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
RETRYABLE_META_ERROR_CODES = frozenset({1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131048,
                                        131056, 133004})
MAX_TEXT_LENGTH = 4096
MAX_PARAMETER_LENGTH = 1024
MAX_CAPTION_LENGTH = 1024
GRAPH_API_VERSION_RE = re.compile(r"^v\d+\.\d+$")


class CloudApiOutboundSender:
    """Sends messages through the WhatsApp Cloud API (graph.facebook.com)."""

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str = "v25.0",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token or not phone_number_id.isdigit():
            raise ValueError("Credenciais da WhatsApp Cloud API ausentes ou inválidas.")
        if not GRAPH_API_VERSION_RE.fullmatch(graph_api_version):
            raise ValueError("Versão da Graph API inválida.")
        base = f"https://graph.facebook.com/{graph_api_version}/{phone_number_id}"
        self._url = f"{base}/messages"
        self._media_url = f"{base}/media"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        )

    def __repr__(self) -> str:  # never show the token
        return "CloudApiOutboundSender()"

    def send_text(self, recipient: str, text: str) -> str | None:
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": text[:MAX_TEXT_LENGTH]},
            }
        )

    def send_template(
        self, recipient: str, name: str, language: str, parameters: list[str]
    ) -> str | None:
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if parameters:
            template["components"] = [
                {
                    "type": "body",
                    # Meta rejects empty parameters and newlines in template variables.
                    "parameters": [
                        {"type": "text", "text": _template_parameter(value)}
                        for value in parameters
                    ],
                }
            ]
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "template",
                "template": template,
            }
        )

    def send_document(
        self, recipient: str, filename: str, content: bytes, mime_type: str, caption: str
    ) -> str | None:
        try:
            upload = self._client.post(
                self._media_url,
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, content, mime_type)},
                headers=self._headers,
            )
        except httpx.TimeoutException as exc:
            raise WhatsAppSendError("media_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise WhatsAppSendError("media_network_error", retryable=True) from exc
        if not upload.is_success:
            raise _classify_error(upload)
        try:
            media_id = upload.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WhatsAppSendError("media_bad_response", retryable=True) from exc
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "document",
                "document": {
                    "id": media_id,
                    "filename": filename,
                    "caption": caption[:MAX_CAPTION_LENGTH],
                },
            }
        )

    def _post(self, body: dict[str, Any]) -> str | None:
        try:
            response = self._client.post(self._url, json=body, headers=self._headers)
        except httpx.TimeoutException as exc:
            # Meta may or may not have accepted it; retrying risks a duplicate reply, which is
            # better than a reply that never arrives.
            raise WhatsAppSendError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise WhatsAppSendError("network_error", retryable=True) from exc

        if response.is_success:
            # Accepted. Never raise from here on: a retry would send the message twice.
            try:
                message_id = response.json()["messages"][0]["id"]
            except (ValueError, KeyError, IndexError, TypeError):
                return None
            return message_id if isinstance(message_id, str) else None
        raise _classify_error(response)

    def close(self) -> None:
        self._client.close()


def _template_parameter(value: str) -> str:
    return " ".join(value.split())[:MAX_PARAMETER_LENGTH] or "-"


def _classify_error(response: httpx.Response) -> WhatsAppSendError:
    meta_code = None
    try:
        error = response.json().get("error") or {}
        meta_code = error.get("code")
    except (ValueError, AttributeError):
        pass
    if isinstance(meta_code, int):
        retryable = meta_code in RETRYABLE_META_ERROR_CODES or response.status_code >= 500
        return WhatsAppSendError(f"meta_{meta_code}", retryable=retryable)
    retryable = response.status_code == 429 or response.status_code >= 500
    return WhatsAppSendError(f"http_{response.status_code}", retryable=retryable)


def build_sender(settings: Any) -> OutboundSender:
    """The real Cloud API sender when credentials are configured, otherwise a local mock.

    Production refuses to start without credentials instead of silently dropping replies.
    """

    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return CloudApiOutboundSender(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            graph_api_version=settings.whatsapp_graph_api_version,
            timeout_seconds=settings.whatsapp_timeout_seconds,
        )
    if settings.environment.strip().lower() == "production":
        raise RuntimeError(
            "Configure LUCROMEI_WHATSAPP_ACCESS_TOKEN e LUCROMEI_WHATSAPP_PHONE_NUMBER_ID."
        )
    logger.warning("whatsapp_mock_sender_in_use")
    return MockOutboundSender()


@dataclass(frozen=True, slots=True)
class WhatsAppStatus:
    provider_message_id: str
    status: str
    occurred_at: datetime | None
    error_code: str | None


DELIVERY_STATUSES = ("sent", "delivered", "read", "failed")


def extract_statuses(payload: dict[str, Any]) -> list[WhatsAppStatus]:
    """Delivery receipts for messages we sent. Recipient numbers are deliberately not kept."""

    statuses: list[WhatsAppStatus] = []
    for value in _change_values(payload):
        for item in value.get("statuses") or []:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            status = item.get("status")
            if not _is_str(message_id) or status not in DELIVERY_STATUSES:
                continue
            error_code = None
            errors = item.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                code = errors[0].get("code")
                error_code = f"meta_{code}" if isinstance(code, int) else None
            statuses.append(
                WhatsAppStatus(
                    provider_message_id=message_id[:255],
                    status=status,
                    occurred_at=_parse_timestamp(item.get("timestamp")),
                    error_code=error_code,
                )
            )
    return statuses
