import json

import httpx
import pytest

from lucromei.whatsapp import CloudApiOutboundSender, WhatsAppSendError


def test_cloud_api_sender_posts_text(monkeypatch):

    def handle(request):
        assert request.method == "POST"
        assert request.url == "https://graph.facebook.com/v25.0/test-id/messages"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert json.loads(request.content) == {
            "messaging_product": "whatsapp",
            "to": "test-recipient",
            "type": "text",
            "text": {"body": "Entrada registrada."},
        }
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        CloudApiOutboundSender("test-token", "test-id").send_text(
            "test-recipient", "Entrada registrada."
        )


@pytest.mark.parametrize("status_code", [302, 401, 503])
def test_cloud_api_sender_raises_on_non_success(monkeypatch, status_code):
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code))
    with httpx.Client(transport=transport) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        sender = CloudApiOutboundSender("test-token", "test-id")
        with pytest.raises(WhatsAppSendError, match=f"HTTP {status_code}") as error:
            sender.send_text("test-recipient", "Entrada registrada.")
        assert "test-id" not in str(error.value)
        assert "test-token" not in repr(sender)
