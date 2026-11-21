"""Abuse guards on the public webhook and response hardening."""

from support import client, post_webhook, reset_state, whatsapp_payload

from lucromei.config import get_settings
from lucromei.ratelimit import SlidingWindowLimiter, client_key


def setup_function() -> None:
    reset_state()


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=1)
    assert not limiter.allow("a", now=2)
    assert limiter.allow("b", now=2)  # per key
    assert limiter.blocked("a", now=30)
    assert limiter.allow("a", now=61.5)  # window slid past the first hit


def test_limiter_memory_is_bounded() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=10)
    for index in range(100):
        limiter.allow(f"k{index}", now=float(index))
    assert len(limiter._hits) <= 10


def test_client_key_uses_the_hop_added_by_cloud_run() -> None:
    assert client_key("1.1.1.1, 9.9.9.9", "10.0.0.1") == "9.9.9.9"
    assert client_key(None, "10.0.0.1") == "10.0.0.1"
    assert client_key("", None) == "unknown"


def test_webhook_rate_limit_per_client() -> None:
    get_settings().webhook_requests_per_minute = 3
    statuses = [post_webhook(whatsapp_payload(f"m{i}", "oi")).status_code for i in range(5)]
    assert statuses == [200, 200, 200, 429, 429]
    get_settings().webhook_requests_per_minute = 600


def test_invalid_signatures_block_the_client_quickly() -> None:
    get_settings().webhook_invalid_signatures_per_minute = 2
    assert post_webhook(whatsapp_payload("x1", "oi"), secret="wrong").status_code == 401
    assert post_webhook(whatsapp_payload("x2", "oi"), secret="wrong").status_code == 401
    # Even a correctly signed request is refused while the budget is spent.
    assert post_webhook(whatsapp_payload("x3", "oi")).status_code == 429
    get_settings().webhook_invalid_signatures_per_minute = 20


def test_oversized_webhook_body_is_refused() -> None:
    get_settings().webhook_max_body_bytes = 2048
    payload = whatsapp_payload("big", "a" * 5000)
    assert post_webhook(payload).status_code == 413
    get_settings().webhook_max_body_bytes = 256 * 1024


def test_security_headers_and_deletion_page() -> None:
    response = client.get("/exclusao-de-dados")
    assert response.status_code == 200
    assert "APAGAR MEUS DADOS" in response.text and "CANCELAR EXCLUSÃO" in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")
    assert client.get("/health").headers["Referrer-Policy"] == "no-referrer"
