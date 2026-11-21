"""Resolve Meta media and transcribe short voice notes without writing audio to disk."""

import asyncio
import io
import json
import logging
import re
import subprocess
import time
import unicodedata
import wave
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from lucromei.config import Settings, is_configured_secret
from lucromei.whatsapp import GRAPH_API_VERSION_RE, MEDIA_ID_RE

logger = logging.getLogger(__name__)
# Force the demuxer associated with the declared type. Only pipe input is available to FFmpeg.
DEMUXERS = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mov",
    "audio/aac": "aac",
    "audio/amr": "amr",
}
MEDIA_HOSTS = {"lookaside.fbsbx.com", "lookaside.facebook.com"}
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
MAX_RESPONSE_BYTES = 256 * 1024
MODEL_PREFIXES = ("gpt-4o-mini-transcribe", "gpt-4o-transcribe")


class AudioError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    duration_seconds: float


def _mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _trusted_media_url(raw: object) -> str:
    if not isinstance(raw, str) or any(ord(char) <= 32 or char == "\\" for char in raw):
        raise AudioError("meta_invalid_url")
    try:
        url = urlsplit(raw)
        if (
            url.scheme == "https"
            and url.netloc in MEDIA_HOSTS
            and not url.fragment
            and url.path == "/whatsapp_business/attachments/"
        ):
            return raw
    except ValueError:
        pass
    raise AudioError("meta_invalid_url")


async def _read_limited(response: httpx.Response, maximum: int, *, code: str) -> bytes:
    # Disallow content encodings so even a compressed response cannot inflate past the cap.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise AudioError("invalid_content_encoding")
    length = response.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal()):
        raise AudioError("invalid_content_length")
    if length is not None and (len(length) > 12 or int(length) > maximum):
        raise AudioError(code)
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        if len(data) + len(chunk) > maximum:
            raise AudioError(code)
        data.extend(chunk)
    return bytes(data)


def _check_status(response: httpx.Response, provider: str, *, expired: bool = False) -> None:
    status = response.status_code
    if not response.is_success:
        retryable = status in {408, 429} or 500 <= status < 600
        retryable |= expired and status in {401, 403, 404, 410}
        raise AudioError(f"{provider}_http_{status}", retryable=retryable)


def _json_object(data: bytes, *, code: str) -> dict:
    try:
        result = json.loads(data)
    except (ValueError, RecursionError):
        raise AudioError(code) from None
    if not isinstance(result, dict):
        raise AudioError(code)
    return result


def _decode(media: bytes, mime: str, max_seconds: int) -> tuple[bytes, float]:
    # Bound output to one sample past the limit, using decoded samples instead of container
    # duration metadata. Rebuild the upload as a clean WAV without source metadata.
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "pipe",
                "-threads",
                "1",
                "-f",
                DEMUXERS[mime],
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-t",
                f"{max_seconds + 1 / SAMPLE_RATE:.6f}",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=media,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except FileNotFoundError:
        raise AudioError("decoder_missing") from None
    except (subprocess.TimeoutExpired, OSError):
        raise AudioError("decoder_failed") from None
    pcm = result.stdout
    if len(pcm) > max_seconds * SAMPLE_RATE * BYTES_PER_SAMPLE:
        raise AudioError("audio_too_long")
    if result.returncode or not pcm or not any(pcm):
        raise AudioError("invalid_audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, BYTES_PER_SAMPLE, SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return output.getvalue(), len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def _transcript(response: dict) -> str:
    raw = response.get("text")
    if not isinstance(raw, str):
        raise AudioError("invalid_transcription")
    text = " ".join(unicodedata.normalize("NFC", raw).split())
    if (
        not text
        or len(text) > 4096
        or not any(char.isalnum() for char in text)
        or any(unicodedata.category(char).startswith("C") for char in text)
        or re.search(r"\b(?:inaudível|ininteligível)\b", text, re.IGNORECASE)
    ):
        raise AudioError("unclear_transcription")
    scores = response.get("logprobs")
    if not isinstance(scores, list) or not scores:
        raise AudioError("invalid_transcription")
    for token in scores:
        value = token.get("logprob") if isinstance(token, dict) else None
        if (
            not isinstance(value, (float, int))
            or isinstance(value, bool)
            or not -1000 <= value <= 0
        ):
            raise AudioError("invalid_transcription")
        if value < -1.5:
            raise AudioError("unclear_transcription")
    return text


class AudioTranscriber:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    def transcribe(self, media_id: str) -> Transcription:
        return asyncio.run(self._transcribe(media_id))

    async def _transcribe(self, media_id: str) -> Transcription:
        settings = self.settings
        if not MEDIA_ID_RE.fullmatch(media_id):
            raise AudioError("invalid_media_id")
        if not (
            is_configured_secret(settings.whatsapp_access_token)
            and is_configured_secret(settings.openai_api_key)
            and MEDIA_ID_RE.fullmatch(settings.whatsapp_phone_number_id)
            and GRAPH_API_VERSION_RE.fullmatch(settings.whatsapp_graph_api_version)
            and any(
                settings.openai_transcription_model == prefix
                or settings.openai_transcription_model.startswith(prefix + "-")
                for prefix in MODEL_PREFIXES
            )
        ):
            raise AudioError("audio_not_configured")

        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
            timeout=None,  # Each provider call is covered by an asyncio deadline.
            headers={"Accept-Encoding": "identity"},
        ) as client:
            try:
                async with asyncio.timeout(settings.whatsapp_timeout_seconds):
                    media, mime = await self._download(client, media_id)
            except (TimeoutError, httpx.TransportError):
                raise AudioError("meta_transport_error", retryable=True) from None
            wav, duration = _decode(media, mime, settings.whatsapp_audio_max_duration_seconds)
            text = await self._request_transcription(client, wav, duration)
        return Transcription(text, duration)

    async def _download(self, client: httpx.AsyncClient, media_id: str) -> tuple[bytes, str]:
        settings = self.settings
        maximum = settings.whatsapp_audio_max_size_bytes
        headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
        async with client.stream(
            "GET",
            f"https://graph.facebook.com/{settings.whatsapp_graph_api_version}/{media_id}",
            params={"phone_number_id": settings.whatsapp_phone_number_id},
            headers=headers,
        ) as response:
            _check_status(response, "meta")
            metadata = _json_object(
                await _read_limited(response, 16_384, code="meta_invalid_metadata"),
                code="meta_invalid_metadata",
            )
        mime = metadata.get("mime_type")
        if metadata.get("id") != media_id or not isinstance(mime, str):
            raise AudioError("meta_invalid_metadata")
        mime = _mime(mime)
        if mime not in DEMUXERS:
            raise AudioError("unsupported_audio")
        size = metadata.get("file_size")
        if type(size) is not int or size <= 0:
            raise AudioError("meta_invalid_metadata")
        if size > maximum:
            raise AudioError("audio_too_large")
        url = _trusted_media_url(metadata.get("url"))
        async with client.stream("GET", url, headers=headers) as response:
            _check_status(response, "media", expired=True)
            if _mime(response.headers.get("content-type", "")) not in {
                mime,
                "application/octet-stream",
            }:
                raise AudioError("unsupported_audio")
            data = await _read_limited(response, maximum, code="audio_too_large")
        if len(data) != size:
            raise AudioError("media_size_mismatch")
        return data, mime

    async def _request_transcription(
        self, client: httpx.AsyncClient, wav: bytes, duration: float
    ) -> str:
        settings = self.settings
        started = time.monotonic()
        outcome = "request_failed"
        try:
            async with asyncio.timeout(settings.openai_transcription_timeout_seconds):
                async with client.stream(
                    "POST",
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    data={
                        "model": settings.openai_transcription_model,
                        "language": "pt",
                        "response_format": "json",
                        "include[]": "logprobs",
                        "prompt": (
                            "Transcrição literal em português do Brasil. Escreva valores em "
                            "algarismos, com vírgula para centavos. Preserve os termos e valores "
                            "falados. Marque trechos incompreensíveis como [inaudível]. "
                            "Não complete palavras ou valores ausentes."
                        ),
                    },
                    files={"file": ("audio.wav", wav, "audio/wav")},
                ) as response:
                    _check_status(response, "openai")
                    text = _transcript(
                        _json_object(
                            await _read_limited(
                                response, MAX_RESPONSE_BYTES, code="invalid_transcription"
                            ),
                            code="invalid_transcription",
                        )
                    )
            outcome = "transcribed"
            return text
        except (TimeoutError, httpx.TransportError):
            outcome = "openai_transport_error"
            raise AudioError(outcome, retryable=True) from None
        except AudioError as exc:
            outcome = exc.code
            raise
        finally:
            logger.info(
                "audio_transcription_request",
                extra={
                    "outcome": outcome,
                    "audio_duration_ms": round(duration * 1000),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                },
            )
