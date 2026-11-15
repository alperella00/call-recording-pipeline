"""Speech-to-text client: every way ElevenLabs can answer, without calling it."""

import json

import httpx
import pytest

from app.pipeline import PermanentError, TransientError
from app.stt import ElevenLabs, Replay

MULTICHANNEL = {
    "transcripts": [
        {"channel_index": 0, "text": "Merhaba.",
         "words": [{"text": "Merhaba.", "start": 1.0, "end": 1.4, "type": "word"},
                   {"text": " ", "start": 1.4, "end": 1.5, "type": "spacing"}]},
        {"channel_index": 1, "text": "Hoş geldiniz.",
         "words": [{"text": "Hoş", "start": 0.0, "end": 0.3, "type": "word"},
                   {"text": "geldiniz.", "start": 0.3, "end": 0.8, "type": "word"}]},
    ]
}


@pytest.fixture
def audio(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    path = tmp_path / "call.wav"
    path.write_bytes(b"RIFF....WAVE")
    return path


def client_answering(status, body=None, headers=None, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body, headers=headers)
    return ElevenLabs(httpx.Client(transport=httpx.MockTransport(handler)))


def test_success_keeps_both_channels_and_only_words(audio):
    seen = []
    result = client_answering(200, MULTICHANNEL, seen=seen).transcribe(audio, "sha", "tr")
    assert result["no_speech"] is False
    assert [c["index"] for c in result["channels"]] == [0, 1]
    assert [w["text"] for w in result["channels"][0]["words"]] == ["Merhaba."]  # spacing dropped
    assert seen[0].headers["xi-api-key"] == "test-key"


def test_silence_is_a_result_not_an_error(audio):
    body = {"detail": {"code": "audio_too_short", "message": "Audio is too short."}}
    assert client_answering(400, body).transcribe(audio, "sha", "tr") == {"no_speech": True, "channels": []}


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_fails_permanently(audio, status):
    with pytest.raises(PermanentError, match=f"API key \\(HTTP {status}, invalid_api_key\\)"):
        client_answering(status, {"detail": {"code": "invalid_api_key"}}).transcribe(audio, "sha", "tr")


def test_the_providers_error_name_is_kept_so_a_used_up_quota_is_not_mistaken_for_a_bad_key(audio):
    # The exact body ElevenLabs sends for an exhausted quota was not observed (it would cost the quota);
    # whatever short name it sends in `detail.code` or `detail.status` ends up in the reason.
    body = {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}
    with pytest.raises(PermanentError, match="HTTP 401, quota_exceeded"):
        client_answering(401, body).transcribe(audio, "sha", "tr")


def test_missing_key_fails_permanently_without_calling(audio, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    seen = []
    with pytest.raises(PermanentError, match="not set"):
        client_answering(200, MULTICHANNEL, seen=seen).transcribe(audio, "sha", "tr")
    assert seen == []


def test_rate_limit_is_retried_after_the_given_delay(audio):
    with pytest.raises(TransientError) as exc:
        client_answering(429, {}, headers={"Retry-After": "7"}).transcribe(audio, "sha", "tr")
    assert exc.value.retry_after == 7


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retried(audio, status):
    with pytest.raises(TransientError):
        client_answering(status, {}).transcribe(audio, "sha", "tr")


def test_network_failure_is_retried(audio):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(TransientError, match="ReadTimeout"):
        ElevenLabs(httpx.Client(transport=httpx.MockTransport(handler))).transcribe(audio, "sha", "tr")


def test_other_client_errors_fail_permanently_with_the_code(audio):
    with pytest.raises(PermanentError, match="invalid_file"):
        client_answering(422, {"detail": {"code": "invalid_file", "message": "x"}}).transcribe(audio, "sha", "tr")


def test_replay_uses_saved_responses_by_hash(tmp_path, audio):
    (tmp_path / "abc.json").write_text(json.dumps(MULTICHANNEL))
    (tmp_path / "silent.json").write_text(json.dumps({"detail": {"code": "audio_too_short"}}))
    replay = Replay(tmp_path)
    assert len(replay.transcribe(audio, "abc", "tr")["channels"]) == 2
    assert replay.transcribe(audio, "silent", "tr")["no_speech"] is True
    with pytest.raises(PermanentError):
        replay.transcribe(audio, "unknown", "tr")
