"""The queue's guarantees: one owner per job, takeover after a worker dies, retries, resume at the failed step."""

import pytest

from app import pipeline
from app.db import connect, tenant_tx
from app.queue import claim, release
from app.reprocess import reprocess
from app.worker import Worker


@pytest.fixture
def jobs(admin):
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs CASCADE")
        admin.execute("INSERT INTO tenants (id, name, crm_base_url) VALUES ('brightsmile', 'BrightSmile', 'http://x')")
        for rec in ("rec_001", "rec_002"):
            admin.execute(
                "INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) VALUES ('brightsmile', %s, 'x', now())", [rec]
            )
            admin.execute("INSERT INTO jobs (tenant_id, recording_id) VALUES ('brightsmile', %s)", [rec])


def job_row(admin, rec="rec_001"):
    return admin.execute(
        "SELECT step, status, attempts, last_error, run_at > now() AS later FROM jobs WHERE recording_id = %s", [rec]
    ).fetchone()


def only(admin, rec):
    """Leave a single job in the queue."""
    admin.execute("DELETE FROM jobs WHERE recording_id <> %s", [rec])


class Steps:
    """Fake steps that record calls and can be told to fail."""

    def __init__(self, fail: dict[str, Exception] | None = None):
        self.calls: list[str] = []
        self.fail = fail or {}

    def as_dict(self):
        def make(name):
            def step(conn, job):
                self.calls.append(name)
                if name in self.fail:
                    raise self.fail.pop(name)
            return step
        return {name: make(name) for name in pipeline.STEPS}


# --- Ownership -------------------------------------------------------------------------------------

def test_two_workers_never_get_the_same_job(jobs):
    with connect("worker") as w1, connect("worker") as w2, connect("worker") as w3:
        a, b = claim(w1, "w1"), claim(w2, "w2")
        assert {a.recording_id, b.recording_id} == {"rec_001", "rec_002"}
        assert claim(w3, "w3") is None


def test_a_worker_holding_a_job_cannot_claim_another(jobs):
    # Advisory locks are re-entrant per connection; without the guard the worker would re-claim its own job.
    with connect("worker") as w:
        assert claim(w, "w") is not None
        with pytest.raises(RuntimeError, match="still holds a job"):
            claim(w, "w")


def test_a_live_workers_job_is_not_taken(jobs, admin):
    only(admin, "rec_001")
    with connect("worker") as w1, connect("worker") as w2:
        assert claim(w1, "w1").recording_id == "rec_001"
        assert claim(w2, "w2") is None


def test_a_killed_workers_job_is_taken_over_and_counted(jobs, admin):
    only(admin, "rec_001")
    w1 = connect("worker")
    assert claim(w1, "w1") is not None
    w1.close()  # what SIGKILL does: the connection goes away without any cleanup

    assert job_row(admin)["status"] == "running"  # still says running ...
    with connect("worker") as w2:
        job = claim(w2, "w2")  # ... but nobody holds the lock, so it is taken over
    assert job.recording_id == "rec_001" and job.attempts == 1
    assert "worker stopped during this step" in job_row(admin)["last_error"]


def test_a_job_that_keeps_killing_workers_ends_as_failed(jobs, admin):
    only(admin, "rec_001")
    admin.execute("UPDATE jobs SET status = 'running', attempts = %s", [pipeline.MAX_ATTEMPTS - 1])
    with connect("worker") as w:
        job = claim(w, "w")
        assert pipeline.run(w, job, Steps().as_dict()) == "failed"
    assert job_row(admin)["status"] == "failed"


# --- Running steps ---------------------------------------------------------------------------------

def test_success_runs_every_step_once_in_order(jobs, admin):
    only(admin, "rec_001")
    steps = Steps()
    with connect("worker") as w:
        assert Worker(steps.as_dict()).run_once(w) == "done"
    assert steps.calls == pipeline.STEPS
    assert job_row(admin)["step"] == "done"


def test_transient_failure_waits_then_resumes_at_the_same_step(jobs, admin):
    only(admin, "rec_001")
    steps = Steps(fail={"mask": pipeline.TransientError("stt 503")})
    with connect("worker") as w:
        assert Worker(steps.as_dict()).run_once(w) == "retry_wait"
        row = job_row(admin)
        assert (row["step"], row["status"], row["attempts"], row["later"]) == ("mask", "retry_wait", 1, True)
        assert claim(w, "w") is None  # not due yet

        admin.execute("UPDATE jobs SET run_at = now()")
        assert Worker(steps.as_dict()).run_once(w) == "done"
    assert steps.calls == ["transcribe", "mask", "mask", "extract", "crm"]  # transcribe not repeated


def test_finished_skips_the_remaining_steps_and_ends_done(jobs, admin):
    only(admin, "rec_001")
    steps = Steps(fail={"transcribe": pipeline.Finished()})
    with connect("worker") as w:
        assert Worker(steps.as_dict()).run_once(w) == "done"
    assert steps.calls == ["transcribe"]
    row = job_row(admin)
    assert (row["step"], row["status"], row["last_error"]) == ("done", "done", None)


def test_deferred_waits_without_using_up_an_attempt(jobs, admin):
    only(admin, "rec_001")
    admin.execute("UPDATE jobs SET step = 'crm', attempts = %s", [pipeline.MAX_ATTEMPTS - 1])
    for _ in range(3):
        steps = Steps(fail={"crm": pipeline.Deferred(5, "earlier call rec_000 may still book a slot")})
        with connect("worker") as w:
            assert Worker(steps.as_dict()).run_once(w) == "retry_wait"
        admin.execute("UPDATE jobs SET run_at = now()")
    row = job_row(admin)
    assert (row["step"], row["status"], row["attempts"]) == ("crm", "retry_wait", pipeline.MAX_ATTEMPTS - 1)
    assert row["last_error"] == "crm: waiting: earlier call rec_000 may still book a slot"


def test_waiting_since_is_kept_across_waits_and_cleared_when_the_step_is_done(jobs, admin):
    only(admin, "rec_001")
    admin.execute("UPDATE jobs SET step = 'crm'")
    first = None
    for _ in range(2):
        steps = Steps(fail={"crm": pipeline.Deferred(5, "earlier call")})
        with connect("worker") as w:
            Worker(steps.as_dict()).run_once(w)
        since = admin.execute("SELECT waiting_since FROM jobs").fetchone()["waiting_since"]
        assert since is not None and since == (first or since)   # set once, not moved by later waits
        first = since
        admin.execute("UPDATE jobs SET run_at = now()")
    with connect("worker") as w:
        assert Worker(Steps().as_dict()).run_once(w) == "done"
    assert admin.execute("SELECT waiting_since FROM jobs").fetchone()["waiting_since"] is None


def test_gives_up_after_max_attempts_and_keeps_the_reason(jobs, admin):
    only(admin, "rec_001")
    admin.execute("UPDATE jobs SET attempts = %s", [pipeline.MAX_ATTEMPTS - 1])
    steps = Steps(fail={"transcribe": pipeline.TransientError("stt 503")})
    with connect("worker") as w:
        assert Worker(steps.as_dict()).run_once(w) == "failed"
    row = job_row(admin)
    assert (row["step"], row["status"]) == ("transcribe", "failed")
    assert "stt 503" in row["last_error"] and f"after {pipeline.MAX_ATTEMPTS} attempts" in row["last_error"]


def test_permanent_failure_fails_at_once_and_reprocess_resumes_there(jobs, admin):
    only(admin, "rec_001")
    steps = Steps(fail={"extract": pipeline.PermanentError("llm key rejected")})
    with connect("worker") as w:
        assert Worker(steps.as_dict()).run_once(w) == "failed"
        assert job_row(admin)["step"] == "extract"

        assert [r["recording_id"] for r in reprocess(w, "brightsmile")] == ["rec_001"]
        assert Worker(steps.as_dict()).run_once(w) == "done"
    assert steps.calls == ["transcribe", "mask", "extract", "extract", "crm"]


def test_reprocess_only_touches_failed_jobs_of_the_given_tenant(jobs, admin):
    admin.execute("UPDATE jobs SET status = 'failed' WHERE recording_id = 'rec_001'")
    with connect("worker") as w:
        assert reprocess(w, "other-tenant") == []
        assert [r["recording_id"] for r in reprocess(w)] == ["rec_001"]
        with pytest.raises(ValueError):
            reprocess(w, recording_id="rec_001")


def test_retry_respects_retry_after_and_the_cap():
    assert pipeline.backoff_seconds(1, retry_after=30) >= 30
    assert all(pipeline.backoff_seconds(n) <= pipeline.BACKOFF_CAP_SECONDS for n in range(1, 20))


def test_lock_is_released_after_the_job(jobs, admin):
    only(admin, "rec_001")
    with connect("worker") as w1, connect("worker") as w2:
        job = claim(w1, "w1")
        release(w1, job.tenant_id, job.recording_id)
        admin.execute("UPDATE jobs SET status = 'queued'")
        assert claim(w2, "w2") is not None


def test_tenant_tx_refuses_to_run_inside_another_transaction(jobs):
    with connect("worker") as w:
        with w.transaction():
            with pytest.raises(RuntimeError):
                with tenant_tx(w, "brightsmile"):
                    pass
