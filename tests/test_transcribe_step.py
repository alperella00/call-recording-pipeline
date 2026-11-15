import pytest

from app.db import tenant_tx
from app.pipeline import Finished, PermanentError, TransientError
from app.queue import Job
from app.steps.transcribe import make_transcribe_step

RESULT = {"no_speech": False, "channels": [{"index": 0, "text": "Merhaba.", "words": []}]}


class FakeTranscriber:
    provider, model = "fake", "fake-1"

    def __init__(self, result=RESULT, error=None):
        self.calls, self.result, self.error = 0, result, error

    def transcribe(self, audio_path, audio_sha256, language):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def recording(admin, tmp_path):
    audio = tmp_path / "rec.wav"
    audio.write_bytes(b"audio")
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs, raw_transcripts, transcripts CASCADE")
        admin.execute("INSERT INTO tenants (id, name, crm_base_url) VALUES ('brightsmile', 'BrightSmile', 'http://x')")
        admin.execute(
            "INSERT INTO recordings (tenant_id, id, audio_path, audio_sha256, recorded_at) "
            "VALUES ('brightsmile', 'rec_001', %s, 'sha', now())", [str(audio)]
        )
    return audio


JOB = Job("brightsmile", "rec_001", "transcribe", 0)


def raw(worker):
    with tenant_tx(worker, "brightsmile"):
        return worker.execute("SELECT provider, content FROM raw_transcripts").fetchall()


def test_stores_the_raw_transcript(worker, recording):
    make_transcribe_step(FakeTranscriber())(worker, JOB)
    assert raw(worker) == [{"provider": "fake", "content": RESULT}]


def test_running_twice_calls_the_provider_once(worker, recording):
    fake = FakeTranscriber()
    step = make_transcribe_step(fake)
    step(worker, JOB)
    step(worker, JOB)  # e.g. the worker died after storing the result, before moving the job on
    assert fake.calls == 1


def test_a_duplicate_upload_is_not_sent_to_speech_to_text(worker, recording, admin):
    admin.execute("INSERT INTO recordings (tenant_id, id, audio_path, audio_sha256, recorded_at, duplicate_of) "
                  "VALUES ('brightsmile', 'rec_021', %s, 'sha', now(), 'rec_001')", [str(recording)])
    fake = FakeTranscriber()
    with pytest.raises(Finished):
        make_transcribe_step(fake)(worker, Job("brightsmile", "rec_021", "transcribe", 0))
    assert fake.calls == 0 and raw(worker) == []


def test_missing_audio_fails_permanently(worker, recording):
    recording.unlink()
    fake = FakeTranscriber()
    with pytest.raises(PermanentError, match="audio file not found"):
        make_transcribe_step(fake)(worker, JOB)
    assert fake.calls == 0


def test_provider_failure_stores_nothing(worker, recording):
    with pytest.raises(TransientError):
        make_transcribe_step(FakeTranscriber(error=TransientError("503")))(worker, JOB)
    assert raw(worker) == []


def test_api_role_cannot_read_raw_transcripts(api, worker, recording):
    make_transcribe_step(FakeTranscriber())(worker, JOB)
    with pytest.raises(Exception, match="permission denied"):
        with tenant_tx(api, "brightsmile"):
            api.execute("SELECT * FROM raw_transcripts")
