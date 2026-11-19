import pytest

from lucromei.config import get_settings


@pytest.fixture
def cloud_run(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("K_SERVICE", "lucromei-test")
    monkeypatch.setenv("LUCROMEI_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "LUCROMEI_DATABASE_URL",
        "postgresql+psycopg://user:sample-password@db.example/test?sslmode=verify-full",
    )
    monkeypatch.setenv("LUCROMEI_WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setenv("LUCROMEI_WHATSAPP_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("LUCROMEI_WHATSAPP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("LUCROMEI_WHATSAPP_PHONE_NUMBER_ID", "123456")
    yield
    get_settings.cache_clear()


def test_cloud_run_accepts_valid_configuration(cloud_run):
    settings = get_settings()
    assert settings.environment == "production"
    assert "sample-password" not in repr(settings)
    assert "test-access-token" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("ENVIRONMENT", None, "LUCROMEI_ENVIRONMENT"),
        ("ENVIRONMENT", "local", "LUCROMEI_ENVIRONMENT"),
        ("WHATSAPP_APP_SECRET", "change-me", "LUCROMEI_WHATSAPP_APP_SECRET"),
        ("WHATSAPP_ACCESS_TOKEN", "", "LUCROMEI_WHATSAPP_ACCESS_TOKEN"),
        ("WHATSAPP_PHONE_NUMBER_ID", "unexpected/path", "LUCROMEI_WHATSAPP_PHONE_NUMBER_ID"),
        ("DATABASE_URL", "sqlite://", "sslmode=verify-full"),
        (
            "DATABASE_URL",
            "postgresql+psycopg://user:sample-password@db.example/test?sslmode=require",
            "sslmode=verify-full",
        ),
    ],
)
def test_cloud_run_rejects_unsafe_configuration(cloud_run, monkeypatch, name, value, error):
    if value is None:
        monkeypatch.delenv(f"LUCROMEI_{name}")
    else:
        monkeypatch.setenv(f"LUCROMEI_{name}", value)
    with pytest.raises(RuntimeError, match=error) as exc:
        get_settings()
    assert "sample-password" not in str(exc.value)
