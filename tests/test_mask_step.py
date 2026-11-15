import pytest
from psycopg.types.json import Jsonb

from app.db import tenant_tx
from app.pipeline import PermanentError
from app.queue import Job
from app.steps.mask import mask


def content(text):
    return {"no_speech": False, "channels": [
        {"index": 0, "text": text, "words": [{"text": w, "start": i, "end": i} for i, w in enumerate(text.split())]}
    ]}


@pytest.fixture
def raw_rows(admin):
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs, raw_transcripts, transcripts CASCADE")
        admin.execute("INSERT INTO tenants (id, name, crm_base_url, assistant_name) VALUES ('brightsmile', 'L', 'x', 'Asya')")
        for rec, text in (("rec_001", "Ozan Kılıç adım."), ("rec_002", "Kerem Emir adım.")):
            admin.execute("INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) VALUES ('brightsmile', %s, 'x', now())", [rec])
            admin.execute(
                "INSERT INTO raw_transcripts (tenant_id, recording_id, provider, model, content) VALUES ('brightsmile', %s, 'p', 'm', %s)",
                [rec, Jsonb(content(text))],
            )


def rows(conn, table):
    with tenant_tx(conn, "brightsmile"):
        return {r["recording_id"]: r for r in conn.execute(f"SELECT * FROM {table}")}


def test_writes_the_masked_transcript_and_deletes_the_raw_one(worker, raw_rows):
    mask(worker, Job("brightsmile", "rec_001", "mask", 0))
    assert rows(worker, "transcripts")["rec_001"]["text"] == "Speaker: [NAME] adım."
    assert "rec_001" not in rows(worker, "raw_transcripts")


def test_each_recording_gets_its_own_transcript(worker, raw_rows):
    # Guards against reading "the" raw transcript of the tenant instead of this recording's.
    mask(worker, Job("brightsmile", "rec_002", "mask", 0))
    assert set(rows(worker, "transcripts")) == {"rec_002"}
    assert set(rows(worker, "raw_transcripts")) == {"rec_001"}


def test_running_twice_is_harmless(worker, raw_rows):
    job = Job("brightsmile", "rec_001", "mask", 0)
    mask(worker, job)
    mask(worker, job)  # raw is gone, transcript exists: nothing to do
    assert len(rows(worker, "transcripts")) == 1


def test_missing_raw_and_transcript_fails_permanently(worker, raw_rows, admin):
    admin.execute("DELETE FROM raw_transcripts")
    with pytest.raises(PermanentError):
        mask(worker, Job("brightsmile", "rec_001", "mask", 0))


def test_api_reads_the_masked_transcript_of_its_own_tenant_only(api, worker, raw_rows):
    mask(worker, Job("brightsmile", "rec_001", "mask", 0))
    with tenant_tx(api, "brightsmile"):
        assert api.execute("SELECT text FROM transcripts").fetchone()["text"] == "Speaker: [NAME] adım."
    with tenant_tx(api, "shopnest"):
        assert api.execute("SELECT text FROM transcripts").fetchall() == []
