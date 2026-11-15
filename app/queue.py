"""The job queue: a table in Postgres plus session-level advisory locks.

Ownership of a job is a Postgres advisory lock held by the worker's *connection*:

- Only one connection can hold a job's lock, so two workers never process the same recording.
- If a worker dies (killed, crashed, container removed), its connection closes and Postgres releases the
  lock at once. A job that says `running` but whose lock nobody holds was abandoned by a dead worker,
  and the next worker that looks for work takes it over. No lease times, no reaper process.
- The worker does all its database writes on that same connection. A worker that lost its connection has
  lost the lock *and* the ability to write, so it cannot overwrite the new owner's progress.

The status columns (`status`, `step`, `worker_id`) are committed normally, so anyone can see them.

Limit: one open connection per worker. Fine up to tens of workers (Postgres allows 100 connections by
default), long before the CRM's 2 requests/second limit makes more workers useless. Beyond that, this file
is the one to change (to time-based leases).
"""

from dataclasses import dataclass

# Jobs that are waiting, or that say `running` but may have been abandoned (checked via the lock).
CLAIMABLE = ["queued", "retry_wait", "running"]
# How many candidates to look at per claim. Must exceed the number of workers, because jobs that are
# genuinely running stay in the candidate list (their lock is held) and must not hide waiting ones.
CANDIDATES = 100


@dataclass
class Job:
    tenant_id: str
    recording_id: str
    step: str
    attempts: int


def _lock_key(tenant_id: str, recording_id: str) -> str:
    return f"job:{tenant_id}:{recording_id}"


def _try_lock(conn, tenant_id: str, recording_id: str) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", [_lock_key(tenant_id, recording_id)]
    ).fetchone()
    return row["ok"]


def release(conn, tenant_id: str, recording_id: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [_lock_key(tenant_id, recording_id)])


def claim(conn, worker_id: str) -> Job | None:
    """Take one job that is due, or None. The caller must `release` it when done."""
    # Advisory locks are re-entrant within a session: a connection that already holds a job would get
    # that job's lock *again*, treat its own job as abandoned, and stack a second lock that one release
    # does not free. One job per connection at a time, enforced.
    held = conn.execute(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
    ).fetchone()["n"]
    if held:
        raise RuntimeError("this connection still holds a job; release it before claiming another")

    candidates = conn.execute(
        """
        SELECT tenant_id, recording_id FROM jobs
        WHERE status = ANY(%s) AND run_at <= now()
        ORDER BY run_at, tenant_id, recording_id
        LIMIT %s
        """,
        [CLAIMABLE, CANDIDATES],
    ).fetchall()

    for c in candidates:
        if not _try_lock(conn, c["tenant_id"], c["recording_id"]):
            continue  # a live worker owns it
        # We hold the lock. Re-check: the job may have finished between our SELECT and the lock.
        # A job still marked `running` had an owner that died: count that as a failed attempt.
        row = conn.execute(
            """
            UPDATE jobs SET
                attempts   = attempts + CASE WHEN status = 'running' THEN 1 ELSE 0 END,
                last_error = CASE WHEN status = 'running'
                                  THEN step || ': worker stopped during this step' ELSE last_error END,
                status     = 'running',
                worker_id  = %s,
                started_at = now(),
                updated_at = now()
            WHERE tenant_id = %s AND recording_id = %s AND status = ANY(%s) AND run_at <= now()
            RETURNING tenant_id, recording_id, step, attempts
            """,
            [worker_id, c["tenant_id"], c["recording_id"], CLAIMABLE],
        ).fetchone()
        if row:
            return Job(**row)
        release(conn, c["tenant_id"], c["recording_id"])
    return None
