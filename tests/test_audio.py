import asyncio
import io
import shutil
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import httpx
import pytest
from sqlalchemy import select
from support import (
    DEFAULT_SENDER,
    TestingSession,
    onboard,
    post_webhook,
    requires_postgres,
    reset_state,
    sent_texts,
    whatsapp_payload,
    worker,
)

from lucromei import audio, processing
from lucromei.config import Settings, get_settings
from lucromei.jobs import JobContext, utc_now
from lucromei.models import InboundEvent, InboundMessage, Transaction
from lucromei.processing import AUDIO_CLARIFY_REPLY, AUDIO_ONBOARDING_REPLY, TRANSCRIBE_AUDIO
from lucromei.whatsapp import extract_inbound_messages

MEDIA_ID = "123456"
PHONE_ID = "654321"
OTHER_SENDER = "5511988888888"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?ticket=private"
AUDIO_BYTES = b"fake ogg bytes"
TRANSCRIPT = "Recebi 150 reais por um corte."
PROVIDER_RESULT = {"text": TRANSCRIPT, "logprobs": [{"logprob": -0.02}]}


def voice_payload(message_id="wamid.voice", *, media_id=MEDIA_ID, sender=DEFAULT_SENDER):
    payload = whatsapp_payload(message_id, None, sender, message_type="audio")
    value = payload["entry"][0]["changes"][0]["value"]
    value["metadata"] = {"phone_number_id": PHONE_ID}
    value["messages"][0]["audio"] = {"id": media_id, "voice": True}
    return payload


@pytest.fixture
def audio_settings(monkeypatch):
    reset_state()
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_audio_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", PHONE_ID)
    monkeypatch.setattr(settings, "whatsapp_access_token", "meta-secret")
    monkeypatch.setattr(settings, "openai_api_key", "openai-secret")
    return settings


def test_extract_audio_media_id_and_routing():
    [message] = extract_inbound_messages(voice_payload())
    assert message.message_type == "audio"
    assert message.media_id == MEDIA_ID
    assert message.phone_number_id == PHONE_ID
    assert message.text is None
    [malformed] = extract_inbound_messages(voice_payload(media_id="../test"))
    assert malformed.media_id is None


class Providers:
    def __init__(self, *, metadata=None, download=None, transcription=None):
        self.calls = []
        self.metadata = metadata or {
            "id": MEDIA_ID,
            "url": MEDIA_URL,
            "mime_type": "audio/ogg; codecs=opus",
            "file_size": len(AUDIO_BYTES),
        }
        self.download = download or httpx.Response(
            200, content=AUDIO_BYTES, headers={"content-type": "audio/ogg"}
        )
        self.transcription = transcription or httpx.Response(200, json=PROVIDER_RESULT)

    def __call__(self, request):
        self.calls.append(request)
        if request.url.host == "graph.facebook.com":
            assert request.url.path == f"/v25.0/{MEDIA_ID}"
            assert request.url.params["phone_number_id"] == PHONE_ID
            assert request.headers["authorization"] == "Bearer meta-secret"
            return httpx.Response(200, json=self.metadata)
        if request.url.host == "lookaside.fbsbx.com":
            assert str(request.url) == MEDIA_URL
            assert request.headers["authorization"] == "Bearer meta-secret"
            return self.download
        assert request.url.host == "api.openai.com"
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer openai-secret"
        assert b"gpt-4o-mini-transcribe" in request.content
        assert b"include[]" in request.content
        assert b'filename="audio.wav"' in request.content
        assert b"private" not in request.content
        return self.transcription


@pytest.fixture
def fake_decoder(monkeypatch):
    def decode(media, mime, maximum):
        assert media == AUDIO_BYTES
        assert mime == "audio/ogg"
        assert maximum == 60
        return b"mock WAV", 1.25

    monkeypatch.setattr(audio, "_decode", decode)


def transcriber(settings, providers):
    return audio.AudioTranscriber(settings, transport=httpx.MockTransport(providers))


def test_secure_download_and_transcription(audio_settings, fake_decoder, caplog):
    providers = Providers(
        transcription=httpx.Response(
            200, json={**PROVIDER_RESULT, "text": "  Recebi  150 reais por um corte.\n"}
        )
    )
    with caplog.at_level("INFO", logger="lucromei.audio"):
        result = transcriber(audio_settings, providers).transcribe(MEDIA_ID)
    assert result == audio.Transcription(TRANSCRIPT, 1.25)
    assert len(providers.calls) == 3
    [metric] = [r for r in caplog.records if r.msg == "audio_transcription_request"]
    assert metric.audio_duration_ms == 1250
    assert metric.latency_ms >= 0
    assert TRANSCRIPT not in metric.getMessage()


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"id": "999"}, "meta_invalid_metadata"),
        ({"file_size": True}, "meta_invalid_metadata"),
        ({"file_size": 4 * 1024 * 1024 + 1}, "audio_too_large"),
        ({"mime_type": "image/jpeg"}, "unsupported_audio"),
        ({"url": "http://lookaside.fbsbx.com/whatsapp_business/attachments/"}, "meta_invalid_url"),
        (
            {"url": "https://lookaside.fbsbx.com@evil.test/whatsapp_business/attachments/"},
            "meta_invalid_url",
        ),
        ({"url": "https://lookaside.fbsbx.com/other/"}, "meta_invalid_url"),
    ],
)
def test_invalid_metadata_and_untrusted_urls(audio_settings, override, code):
    providers = Providers()
    providers.metadata.update(override)
    with pytest.raises(audio.AudioError, match=code):
        transcriber(audio_settings, providers).transcribe(MEDIA_ID)
    assert len(providers.calls) == 1


@pytest.mark.parametrize(
    ("status", "headers", "code", "retryable"),
    [
        (302, {"location": "https://evil.test"}, "media_http_302", False),
        (404, {}, "media_http_404", True),
        (200, {"content-type": "application/json"}, "unsupported_audio", False),
        (
            200,
            {"content-type": "audio/ogg", "content-length": "999999999"},
            "audio_too_large",
            False,
        ),
        (
            200,
            {"content-type": "audio/ogg", "content-length": "a"},
            "invalid_content_length",
            False,
        ),
    ],
)
def test_invalid_download(audio_settings, status, headers, code, retryable):
    providers = Providers(download=httpx.Response(status, headers=headers, content=AUDIO_BYTES))
    with pytest.raises(audio.AudioError, match=code) as error:
        transcriber(audio_settings, providers).transcribe(MEDIA_ID)
    assert error.value.retryable is retryable
    assert len(providers.calls) == 2


def test_streamed_size_is_bounded(audio_settings, monkeypatch):
    monkeypatch.setattr(audio_settings, "whatsapp_audio_max_size_bytes", len(AUDIO_BYTES))

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield AUDIO_BYTES
            yield b"extra"

    providers = Providers(
        download=httpx.Response(
            200,
            headers={"content-type": "audio/ogg"},
            stream=Stream(),
        )
    )
    with pytest.raises(audio.AudioError, match="audio_too_large"):
        transcriber(audio_settings, providers).transcribe(MEDIA_ID)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json={"text": ""}), "unclear_transcription"),
        (httpx.Response(200, json={"text": TRANSCRIPT}), "invalid_transcription"),
        (
            httpx.Response(200, json={**PROVIDER_RESULT, "logprobs": [{"logprob": -3}]}),
            "unclear_transcription",
        ),
        (
            httpx.Response(200, json={**PROVIDER_RESULT, "text": "Recebi [inaudível]"}),
            "unclear_transcription",
        ),
        (httpx.Response(200, text="{"), "invalid_transcription"),
        (httpx.Response(503), "openai_http_503"),
    ],
)
def test_provider_response_validation(audio_settings, fake_decoder, response, code):
    providers = Providers(transcription=response)
    with pytest.raises(audio.AudioError, match=code) as error:
        transcriber(audio_settings, providers).transcribe(MEDIA_ID)
    assert error.value.retryable is (response.status_code == 503)


def test_provider_total_deadline(audio_settings, fake_decoder, monkeypatch):
    providers = Providers()
    monkeypatch.setattr(audio_settings, "openai_transcription_timeout_seconds", 0.01)

    async def stalled(request):
        if request.url.host == "api.openai.com":
            await asyncio.sleep(10)
        return providers(request)

    with pytest.raises(audio.AudioError, match="openai_transport_error") as error:
        transcriber(audio_settings, stalled).transcribe(MEDIA_ID)
    assert error.value.retryable


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires FFmpeg")
def test_decoded_ogg_duration_and_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for length in (0.5, 1.0, 1.1):
        recording = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                str(length),
                "-c:a",
                "libopus",
                "-f",
                "ogg",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout
        if length > 1:
            with pytest.raises(audio.AudioError, match="audio_too_long"):
                audio._decode(recording, "audio/ogg", 1)
        else:
            data, duration = audio._decode(recording, "audio/ogg", 1)
            assert duration == length
            with wave.open(io.BytesIO(data)) as wav:
                assert wav.getframerate() == 16_000
                assert wav.getnchannels() == 1
    with pytest.raises(audio.AudioError, match="invalid_audio"):
        audio._decode(b"bogus OGG", "audio/ogg", 1)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        (TRANSCRIPT, "Entrada registrada: R$ 150,00", 1),
        ("Gastei 80 reais em produtos.", "Despesa registrada: R$ 80,00", 1),
        ("Retirei 500 reais para mim.", "Retirada pessoal registrada: R$ 500,00", 1),
        ("Resumo do mês?", "Resumo", 0),
        ("Recebi 200 reais e gastei 50 reais no transporte.", "uma operação por mensagem", 0),
    ],
)
def test_audio_reuses_processing_with_duplicate_delivery(
    audio_settings, fake_decoder, text, expected, count
):
    business_id = onboard()
    other_business = onboard(OTHER_SENDER)
    providers = Providers(transcription=httpx.Response(200, json={**PROVIDER_RESULT, "text": text}))
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    assert post_webhook(voice_payload()).json()["accepted"] == 1
    assert not providers.calls  # webhook acknowledges before download
    assert post_webhook(voice_payload()).json()["duplicates"] == 1
    runner.drain()
    assert post_webhook(voice_payload()).json()["duplicates"] == 1
    runner.drain()
    assert len(providers.calls) == 3
    [reply] = sent_texts()
    assert expected in reply
    with TestingSession() as session:
        rows = session.scalars(select(Transaction)).all()
        assert len(rows) == count
        assert all(
            row.business_id == business_id and row.business_id != other_business for row in rows
        )
        event = session.scalar(select(InboundEvent))
        assert event.status == "processed"
        assert event.media_id is None
        if count:
            assert session.scalar(select(InboundMessage)).raw_text == ""


def test_signature_routing_and_onboarding_precede_provider_call(audio_settings):
    onboard(OTHER_SENDER)
    providers = Providers()
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    assert post_webhook(voice_payload(), secret="wrong").status_code == 401
    wrong_route = voice_payload()
    wrong_route["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
    assert post_webhook(wrong_route).json()["accepted"] == 0
    assert post_webhook(voice_payload()).json()["accepted"] == 1
    runner.drain()
    assert not providers.calls
    assert sent_texts() == [AUDIO_ONBOARDING_REPLY]


def test_disabled_audio_keeps_text_processing(audio_settings, monkeypatch):
    monkeypatch.setattr(audio_settings, "whatsapp_audio_enabled", False)
    onboard()
    runner = worker()
    assert runner.transcriber is None
    post_webhook(voice_payload())
    post_webhook(whatsapp_payload("wamid.text", TRANSCRIPT, timestamp="1795032001"))
    runner.drain()
    assert any("só entendo mensagens de texto" in reply for reply in sent_texts())
    assert any("Entrada registrada" in reply for reply in sent_texts())


@pytest.mark.parametrize("recover", [True, False])
def test_expired_media_is_resolved_again_and_retries_are_bounded(
    audio_settings, fake_decoder, recover
):
    onboard()
    providers = Providers(download=httpx.Response(404))
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    post_webhook(voice_payload())
    current = utc_now()
    for attempt in range(3):
        [job] = runner.queue.claim(limit=1, now=current)
        assert job.kind == TRANSCRIBE_AUDIO
        status = runner.queue.run(job, JobContext(current, runner.context().services))
        if attempt == 0 and recover:
            assert status == "pending"
            providers.download = httpx.Response(
                200,
                headers={"content-type": "audio/ogg"},
                content=AUDIO_BYTES,
            )
        if status != "pending":
            break
        current += timedelta(minutes=1)
    runner.drain()
    hosts = [request.url.host for request in providers.calls]
    assert hosts.count("graph.facebook.com") == (2 if recover else 3)
    assert hosts.count("api.openai.com") == int(recover)
    [reply] = sent_texts()
    assert ("Entrada registrada" in reply) if recover else (reply == AUDIO_CLARIFY_REPLY)
    with TestingSession() as session:
        assert session.scalar(select(InboundEvent)).media_id is None


def test_duration_limit_cannot_exceed_sixty_seconds():
    with pytest.raises(ValueError):
        Settings(whatsapp_audio_max_duration_seconds=61)


def test_transcription_is_checkpointed_before_financial_retry(
    audio_settings, fake_decoder, monkeypatch
):
    onboard()
    providers = Providers(transcription=httpx.Response(503))
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    post_webhook(voice_payload())
    now = utc_now()
    [transcription] = runner.queue.claim(limit=1, now=now)
    assert runner.queue.run(transcription, JobContext(now, runner.context().services)) == "pending"
    providers.transcription = httpx.Response(200, json=PROVIDER_RESULT)
    now += timedelta(minutes=1)
    [transcription] = runner.queue.claim(limit=1, now=now)
    assert runner.queue.run(transcription, JobContext(now, runner.context().services)) == "done"
    with TestingSession() as session:
        assert session.scalar(select(InboundEvent)).text == TRANSCRIPT
        assert session.scalar(select(Transaction)) is None

    def interrupted(*args):
        raise RuntimeError("simulated ledger failure")

    [financial_job] = runner.queue.claim(limit=1, now=now)
    with monkeypatch.context() as patch:
        patch.setattr(processing, "build_reply", interrupted)
        assert (
            runner.queue.run(financial_job, JobContext(now, runner.context().services)) == "pending"
        )
    now += timedelta(minutes=1)
    [financial_job] = runner.queue.claim(limit=1, now=now)
    assert runner.queue.run(financial_job, JobContext(now, runner.context().services)) == "done"
    assert [request.url.host for request in providers.calls].count("api.openai.com") == 2
    with TestingSession() as session:
        [transaction] = session.scalars(select(Transaction)).all()
        assert transaction.amount_cents == 15_000


def test_audio_duration_reply_in_portuguese(audio_settings, monkeypatch):
    onboard()

    def too_long(*args):
        raise audio.AudioError("audio_too_long")

    monkeypatch.setattr(audio, "_decode", too_long)
    providers = Providers()
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    post_webhook(voice_payload())
    runner.drain()
    assert sent_texts() == [
        "Seu áudio ficou um pouco longo. Envie um áudio de até 60 segundos, por favor."
    ]
    assert len(providers.calls) == 2


@requires_postgres
def test_live_transcription_cannot_be_reclaimed_after_lease_expiry(audio_settings, fake_decoder):
    onboard()
    providers = Providers()
    in_flight = Event()
    release = Event()

    async def blocking_provider(request):
        if request.url.host == "api.openai.com":
            in_flight.set()
            assert release.wait(5)
        return providers(request)

    runner = worker()
    runner.transcriber = transcriber(audio_settings, blocking_provider)
    post_webhook(voice_payload())
    now = utc_now()
    [job] = runner.queue.claim(limit=1, now=now)
    with ThreadPoolExecutor() as executor:
        result = executor.submit(runner.queue.run, job, JobContext(now, runner.context().services))
        try:
            assert in_flight.wait(5)
            assert runner.queue.claim(limit=1, now=now + timedelta(minutes=3)) == []
        finally:
            release.set()
    assert result.result() == "done"
    assert [request.url.host for request in providers.calls].count("api.openai.com") == 1


def test_ambiguous_audio_uses_existing_confirmation(audio_settings, fake_decoder):
    onboard()
    providers = Providers(
        transcription=httpx.Response(
            200, json={**PROVIDER_RESULT, "text": "Paguei 300 para Maria."}
        )
    )
    runner = worker()
    runner.transcriber = transcriber(audio_settings, providers)
    post_webhook(voice_payload())
    runner.drain()
    with TestingSession() as session:
        assert session.scalar(select(Transaction)) is None
    [question] = sent_texts()
    assert "R$ 300,00" in question
    assert "despesa" in question.lower()
    post_webhook(whatsapp_payload("wamid.answer", "despesa", timestamp="1795032001"))
    runner.drain()
    with TestingSession() as session:
        [transaction] = session.scalars(select(Transaction)).all()
        assert transaction.amount_cents == 30_000
        assert transaction.transaction_type == "expense"
        assert session.scalar(select(InboundMessage)).raw_text == ""
    assert len(providers.calls) == 3
