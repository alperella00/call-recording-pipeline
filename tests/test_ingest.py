import pytest

from app.db import tenant_tx
from app.ingest import ingest_recording, resolve_audio


@pytest.fixture
def empty_brightsmile(admin):
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs CASCADE")
        admin.execute("INSERT INTO tenants (id, name, crm_base_url) VALUES ('brightsmile', 'BrightSmile', 'http://x')")


def rec(id_, file, at):
    return {"id": id_, "tenant_id": "brightsmile", "file": file, "recorded_at": at}


def state(worker):
    with tenant_tx(worker, "brightsmile"):
        return {
            r["id"]: (r["duplicate_of"], r["status"], r["last_error"])
            for r in worker.execute(
                "SELECT r.id, r.duplicate_of, j.status, j.last_error FROM recordings r "
                "JOIN jobs j ON j.tenant_id = r.tenant_id AND j.recording_id = r.id"
            )
        }


def test_rerun_registers_nothing_twice(worker, empty_brightsmile, tmp_path):
    (tmp_path / "a.wav").write_bytes(b"audio a")
    r = rec("rec_001", "a.wav", "2026-10-30T08:00:00+03:00")
    assert ingest_recording(worker, tmp_path, r) == "queued"
    assert ingest_recording(worker, tmp_path, r) == "already registered"
    assert state(worker) == {"rec_001": (None, "queued", None)}


@pytest.mark.parametrize("order", ["original first", "copy first"])
def test_same_audio_twice_marks_the_later_call_as_duplicate(worker, empty_brightsmile, tmp_path, order):
    (tmp_path / "a.wav").write_bytes(b"same bytes")
    (tmp_path / "b.wav").write_bytes(b"same bytes")
    original = rec("rec_001", "a.wav", "2026-10-30T08:49:28+03:00")
    copy = rec("rec_021", "b.wav", "2026-10-30T12:46:06+03:00")
    for r in ([original, copy] if order == "original first" else [copy, original]):
        ingest_recording(worker, tmp_path, r)

    s = state(worker)
    assert s["rec_001"][0] is None
    assert s["rec_021"][0] == "rec_001"


def test_missing_audio_is_registered_as_failed_not_skipped(worker, empty_brightsmile, tmp_path):
    outcome = ingest_recording(worker, tmp_path, rec("rec_404", "missing.wav", "2026-10-30T08:00:00+03:00"))
    assert "FAILED" in outcome
    assert state(worker) == {"rec_404": (None, "failed", "audio file not found")}


def test_manifest_cannot_point_outside_its_folder(tmp_path):
    with pytest.raises(ValueError):
        resolve_audio(tmp_path, "../../etc/passwd")
