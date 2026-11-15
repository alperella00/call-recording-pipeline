"""Runs a claimed job through the pipeline steps, and decides what happens when a step fails.

Steps run in a fixed order. A job remembers the step it is at, so after a failure, a retry, a killed worker
or a `reprocess`, it continues from that step and never repeats the ones that already succeeded.

Delivery is at-least-once: a worker can die after a step wrote its result but before the job moved on.
Every step must therefore be idempotent (safe to run twice). Each step reads its input from what the
previous step stored and checks whether its own output already exists.

Failures:
- TransientError: the third party is down, slow or rate-limiting. Retried later with exponential backoff,
  up to MAX_ATTEMPTS, then the job is marked failed (and stays reprocessable).
- PermanentError: retrying cannot help (bad API key, invalid input). Failed at once with the reason.
- Deferred: not a failure, the step cannot run *yet* (e.g. an earlier call must reach the CRM first). The
  job waits and is picked up again later without using up an attempt. `jobs.waiting_since` records when the
  waiting started at this step, so whoever raises it can bound the total wait.
- Finished: not a failure, the job has nothing left to do (e.g. the audio is a duplicate of an earlier upload).
  The remaining steps are skipped and the job is done.
- Any other exception is a bug; it is treated as transient so a flaky bug cannot drop a recording, and it
  ends as failed with its type if it keeps happening.
"""

import random
from collections.abc import Callable

from app.queue import Job

STEPS = ["transcribe", "mask", "extract", "crm"]
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0

StepFn = Callable[..., None]  # step(conn, job) -> None


class TransientError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    pass


class Finished(Exception):
    pass


class Deferred(Exception):
    def __init__(self, seconds: float, reason: str):
        super().__init__(reason)
        self.seconds = seconds


def next_step(step: str) -> str:
    i = STEPS.index(step)
    return STEPS[i + 1] if i + 1 < len(STEPS) else "done"


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with full jitter, never earlier than the server's Retry-After."""
    delay = random.uniform(0, min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)))
    return max(delay, retry_after or 0)


def _describe(exc: BaseException) -> str:
    # Messages are built by our own code from status codes and error names, never from transcript text.
    return f"{type(exc).__name__}: {exc}"[:500]


def _update_job(conn, job: Job, sql: str, params: list) -> None:
    conn.execute(
        f"UPDATE jobs SET {sql}, updated_at = now() WHERE tenant_id = %s AND recording_id = %s",
        [*params, job.tenant_id, job.recording_id],
    )


def run(conn, job: Job, steps: dict[str, StepFn]) -> str:
    """Run the job from its current step until done or a failure. Returns the final status."""
    step = job.step
    attempts = job.attempts
    if attempts >= MAX_ATTEMPTS:
        # Workers kept dying on this step (e.g. a file that crashes the process): stop retrying.
        _update_job(conn, job, "status = 'failed', last_error = %s",
                    [f"{step}: worker stopped during this step {attempts} times"])
        return "failed"
    while step != "done":
        try:
            steps[step](conn, job)
        except Finished:
            break
        except Deferred as exc:
            _update_job(conn, job,
                        "status = 'retry_wait', last_error = %s, run_at = now() + make_interval(secs => %s), "
                        "waiting_since = COALESCE(waiting_since, now())",
                        [f"{step}: waiting: {exc}", exc.seconds])
            return "retry_wait"
        except PermanentError as exc:
            _update_job(conn, job, "status = 'failed', last_error = %s", [f"{step}: {_describe(exc)}"])
            return "failed"
        except Exception as exc:  # TransientError, or a bug
            attempts += 1
            error = f"{step}: {_describe(exc)}"
            if attempts >= MAX_ATTEMPTS:
                _update_job(conn, job, "status = 'failed', attempts = %s, last_error = %s",
                            [attempts, f"{error} (after {attempts} attempts)"])
                return "failed"
            delay = backoff_seconds(attempts, getattr(exc, "retry_after", None))
            _update_job(conn, job,
                        "status = 'retry_wait', attempts = %s, last_error = %s, "
                        "run_at = now() + make_interval(secs => %s)",
                        [attempts, error, delay])
            return "retry_wait"

        step = next_step(step)
        attempts = 0
        _update_job(conn, job, "step = %s, attempts = 0, last_error = NULL, waiting_since = NULL", [step])

    _update_job(conn, job, "step = 'done', status = 'done', attempts = 0, last_error = NULL, waiting_since = NULL", [])
    return "done"
