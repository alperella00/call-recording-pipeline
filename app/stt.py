"""Speech-to-text providers.

A provider turns an audio file into a provider-independent result:

    {"no_speech": bool, "channels": [{"index": 0, "text": "...", "words": [{"text", "start", "end", "logprob"}]}]}

and raises TransientError (retry later) or PermanentError (retrying won't help) from app.pipeline.

Providers (STT_PROVIDER):
- elevenlabs (default): ElevenLabs Scribe, one transcript per audio channel. Needs ELEVENLABS_API_KEY.
- replay: returns saved ElevenLabs responses, looked up by the audio's sha256, from STT_REPLAY_DIR. Used in
  development so the same files are not paid for again. A file with no saved response fails permanently.
"""

import json
import os
from pathlib import Path
from typing import Protocol

import httpx

from app.pipeline import PermanentError, TransientError

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class Transcriber(Protocol):
    provider: str
    model: str

    def transcribe(self, audio_path: Path, audio_sha256: str, language: str) -> dict: ...


def parse_elevenlabs(payload: dict) -> dict:
    """Normalise an ElevenLabs response (multichannel or single channel)."""
    channels = payload["transcripts"] if "transcripts" in payload else [payload]
    return {
        "no_speech": False,
        "channels": [
            {
                "index": ch.get("channel_index") or 0,
                "text": ch.get("text", ""),
                "words": [
                    # logprob: how sure the model is about this word (0 = certain); used to flag likely mishearings
                    {"text": w["text"], "start": w["start"], "end": w["end"], "logprob": w.get("logprob")}
                    for w in ch.get("words", [])
                    if w.get("type") == "word"
                ],
            }
            for ch in channels
        ],
    }


NO_SPEECH = {"no_speech": True, "channels": []}


def classify_elevenlabs_error(status: int, body: dict | None, retry_after: str | None) -> dict:
    """Map an ElevenLabs error response to a result or an exception. Messages carry codes, not content."""
    detail = (body or {}).get("detail")
    code = detail.get("code") if isinstance(detail, dict) else None
    # The error name the provider sent, if any (e.g. a used-up quota rather than a wrong key). Only short
    # identifiers are kept, never free text.
    name = code or (detail.get("status") if isinstance(detail, dict) else None)
    name = name if isinstance(name, str) and len(name) <= 40 else None
    if status == 400 and code == "audio_too_short":
        return NO_SPEECH  # silence: a result, not an error; retrying would never change it
    if status in (401, 403):
        raise PermanentError(f"speech-to-text rejected the API key (HTTP {status}{', ' + name if name else ''})")
    if status == 429:
        raise TransientError("speech-to-text rate limited (HTTP 429)",
                             retry_after=float(retry_after) if retry_after else None)
    if status >= 500:
        raise TransientError(f"speech-to-text unavailable (HTTP {status})")
    raise PermanentError(f"speech-to-text refused the request (HTTP {status}, {code or 'no code'})")


class ElevenLabs:
    provider = "elevenlabs"
    model = "scribe_v1"

    def __init__(self, client: httpx.Client | None = None):
        # The upload of a few MB plus transcription can take a while; connecting should not.
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=10))

    def transcribe(self, audio_path: Path, audio_sha256: str, language: str) -> dict:
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            raise PermanentError("ELEVENLABS_API_KEY is not set")
        try:
            with audio_path.open("rb") as f:
                response = self.client.post(
                    ELEVENLABS_URL,
                    headers={"xi-api-key": api_key},
                    data={"model_id": self.model, "language_code": language,
                          "use_multi_channel": "true", "timestamps_granularity": "word"},
                    files={"file": (audio_path.name, f, "audio/wav")},
                )
        except httpx.TransportError as exc:  # timeouts, refused or dropped connections
            raise TransientError(f"speech-to-text unreachable ({type(exc).__name__})") from None

        if response.status_code == 200:
            return parse_elevenlabs(response.json())
        try:
            body = response.json()
        except ValueError:
            body = None
        return classify_elevenlabs_error(response.status_code, body, response.headers.get("retry-after"))


class Replay:
    """Saved ElevenLabs responses, named <audio sha256>.json. Development only."""

    provider = "elevenlabs"
    model = "scribe_v1"

    def __init__(self, directory: Path):
        self.directory = directory

    def transcribe(self, audio_path: Path, audio_sha256: str, language: str) -> dict:
        saved = self.directory / f"{audio_sha256}.json"
        if not saved.is_file():
            raise PermanentError("replay: no saved response for this audio")
        payload = json.loads(saved.read_text())
        if "transcripts" in payload or "words" in payload:
            return parse_elevenlabs(payload)
        return classify_elevenlabs_error(payload.get("status", 400), payload, None)


def from_env() -> Transcriber:
    provider = os.environ.get("STT_PROVIDER", "elevenlabs")
    if provider == "elevenlabs":
        return ElevenLabs()
    if provider == "replay":
        return Replay(Path(os.environ["STT_REPLAY_DIR"]))
    raise SystemExit(f"unknown STT_PROVIDER {provider!r}")
