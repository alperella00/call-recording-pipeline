"""Step 4: send the call's complaint and book its meeting in the tenant's CRM.

Three rules make this safe with several workers, a flaky CRM and workers that get killed:

1. One writer per CRM (the "lane"). The step holds a session advisory lock on the CRM's URL while it talks
   to it. That keeps requests under the rate limit and makes bookings happen one at a time. If the worker
   dies, its connection closes and the lane is free at once. The lock uses the two-integer key space,
   so it never collides with job locks (one bigint key, app/queue.py), and it is released before the job is.

2. Write down, then send. Before a POST, its idempotency key and exact body are stored in `crm_actions` as
   `pending`. Whoever runs the step next (a retry, or a worker taking over from a dead one) re-sends a
   pending action unchanged, with the same key, *before* doing anything else. A new slot is chosen only
   after the previous attempt got a definite answer. Without this, a request delayed in flight could land
   after the next worker looked at the calendar, that worker would pick another slot, and the call would
   end up with two bookings.
   When there is no pending action (first attempt, or the database was reset), the CRM's own list
   (`GET /bookings`, `GET /complaints`) is checked for the call first, and an existing entry is adopted.

3. First come, first served. A booking waits (Deferred, without using up an attempt) while an earlier call
   of the same tenant may still book, i.e. it is not yet extracted or its meeting is still pending, but at
   most WAIT_FOR_EARLIER_SECONDS after this job started waiting in this run (`jobs.waiting_since`, so old
   requests resumed by a reprocess are still served in order). A failed earlier call is not waited for.
   So the order holds while things are healthy, and gives way when they are not.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app import crm
from app.db import tenant_tx
from app.masking import caller_company
from app.pipeline import Deferred, PermanentError, StepFn, TransientError
from app.queue import Job

LANE_LOCK_SPACE = 1                 # first integer of the two-integer advisory lock key
WAIT_FOR_EARLIER_SECONDS = 120
WAIT_RECHECK_SECONDS = 5
LANE_BUSY_RETRY_SECONDS = 2
MAX_SLOT_CONFLICTS = 5

ClientFactory = Callable[..., crm.CrmClient]   # (conn, crm_base_url) -> CrmClient


def default_client(conn, crm_base_url: str) -> crm.CrmClient:
    return crm.CrmClient(crm_base_url, pace=crm.db_pacer(conn, crm_base_url))


@dataclass
class Action:
    key: str
    body: str


def make_crm_step(make_client: ClientFactory = default_client) -> StepFn:
    def crm_step(conn, job: Job) -> None:
        ctx = _load(conn, job)
        if ctx is None:
            raise PermanentError("recording not found")
        if ctx["duplicate_of"]:
            return  # same audio as an earlier recording, which sends the actions
        meeting, complaint = ctx["meeting"], ctx["complaint"]
        need_complaint = complaint is not None and complaint["status"] == "pending"
        need_booking = meeting is not None and meeting["status"] == "pending"
        if not (need_complaint or need_booking):
            return

        if not _try_lane(conn, ctx["crm_base_url"]):
            raise Deferred(LANE_BUSY_RETRY_SECONDS, "another worker is writing to this CRM")
        try:
            client = make_client(conn, ctx["crm_base_url"])
            if need_complaint:
                _send_complaint(conn, job, client, complaint)
            if need_booking:
                _wait_for_earlier_calls(conn, job)
                _book(conn, job, client, ctx, meeting)
        finally:
            _release_lane(conn, ctx["crm_base_url"])

    return crm_step


# --- Reading state ---------------------------------------------------------------------------------------

def _load(conn, job: Job) -> dict | None:
    with tenant_tx(conn, job.tenant_id):
        row = conn.execute(
            """
            SELECT r.duplicate_of, r.recorded_at, t.crm_base_url, t.timezone
            FROM recordings r JOIN tenants t ON t.id = r.tenant_id
            WHERE r.id = %s
            """,
            [job.recording_id],
        ).fetchone()
        if row is None:
            return None
        row["meeting"] = conn.execute("SELECT * FROM meetings WHERE recording_id = %s",
                                      [job.recording_id]).fetchone()
        row["complaint"] = conn.execute("SELECT * FROM complaints WHERE recording_id = %s",
                                        [job.recording_id]).fetchone()
        transcript = conn.execute("SELECT turns FROM transcripts WHERE recording_id = %s",
                                  [job.recording_id]).fetchone()
        row["turns"] = transcript["turns"] if transcript else []
    return row


def _open_action(conn, job: Job, kind: str) -> Action | None:
    with tenant_tx(conn, job.tenant_id):
        row = conn.execute(
            "SELECT idempotency_key, request_body FROM crm_actions "
            "WHERE recording_id = %s AND kind = %s AND status = 'pending'",
            [job.recording_id, kind],
        ).fetchone()
    return Action(row["idempotency_key"], row["request_body"]) if row else None


def _record_action(conn, job: Job, kind: str, key: str, body: str) -> Action:
    with tenant_tx(conn, job.tenant_id):
        inserted = conn.execute(
            """
            INSERT INTO crm_actions (tenant_id, recording_id, kind, idempotency_key, request_body, status)
            VALUES (%s, %s, %s, %s, %s, 'pending') ON CONFLICT DO NOTHING RETURNING 1
            """,
            [job.tenant_id, job.recording_id, kind, key, body],
        ).fetchone()
    if not inserted:
        raise PermanentError(f"{kind} idempotency key was already used for this call")
    return Action(key, body)


def _finish_action(tx, job: Job, action: Action, status: str, resp: crm.Response) -> None:
    tx.execute(
        """
        UPDATE crm_actions SET status = %s, http_status = %s, crm_id = %s, updated_at = now()
        WHERE recording_id = %s AND idempotency_key = %s
        """,
        [status, resp.status, resp.body.get("id"), job.recording_id, action.key],
    )


def _post(conn, job: Job, client: crm.CrmClient, path: str, action: Action) -> crm.Response:
    with tenant_tx(conn, job.tenant_id):
        conn.execute("UPDATE crm_actions SET attempts = attempts + 1, updated_at = now() "
                     "WHERE recording_id = %s AND idempotency_key = %s", [job.recording_id, action.key])
    return client.post(path, action.key, action.body)


# --- The lane ----------------------------------------------------------------------------------------------

def _try_lane(conn, crm_base_url: str) -> bool:
    return conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS ok",
                        [LANE_LOCK_SPACE, crm_base_url]).fetchone()["ok"]


def _release_lane(conn, crm_base_url: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", [LANE_LOCK_SPACE, crm_base_url])


def _wait_for_earlier_calls(conn, job: Job) -> None:
    with tenant_tx(conn, job.tenant_id):
        row = conn.execute(
            """
            SELECT r.id,
                   COALESCE(me_j.waiting_since, now()) > now() - make_interval(secs => %(limit)s) AS still_waiting
            FROM recordings me_r
            JOIN jobs me_j ON me_j.tenant_id = me_r.tenant_id AND me_j.recording_id = me_r.id
            JOIN recordings r ON r.recorded_at < me_r.recorded_at AND r.duplicate_of IS NULL
            JOIN jobs j ON j.tenant_id = r.tenant_id AND j.recording_id = r.id
            LEFT JOIN meetings m ON m.recording_id = r.id
            WHERE me_r.id = %(rec)s
              AND j.status <> 'failed'
              AND (j.step IN ('transcribe', 'mask', 'extract') OR m.status = 'pending')
            ORDER BY r.recorded_at
            LIMIT 1
            """,
            {"rec": job.recording_id, "limit": WAIT_FOR_EARLIER_SECONDS},
        ).fetchone()
    if row and row["still_waiting"]:
        raise Deferred(WAIT_RECHECK_SECONDS, f"earlier call {row['id']} may still book a slot")


# --- Complaint ------------------------------------------------------------------------------------------

def _send_complaint(conn, job: Job, client: crm.CrmClient, complaint: dict) -> None:
    action = _open_action(conn, job, "complaint")
    if action is None:
        existing = _find_call(client.get("/complaints", paced=False)["complaints"], job.recording_id)
        if existing:
            _mark_complaint_sent(conn, job, existing["id"])
            return
        action = _record_action(conn, job, "complaint", crm.complaint_key(job.tenant_id, job.recording_id),
                                crm.complaint_body(job.recording_id, complaint["summary"]))

    resp = _post(conn, job, client, "/complaints", action)
    if resp.status in (200, 201):
        with tenant_tx(conn, job.tenant_id) as tx:
            _finish_action(tx, job, action, "succeeded", resp)
        _mark_complaint_sent(conn, job, resp.body.get("id"))
        return
    with tenant_tx(conn, job.tenant_id) as tx:
        _finish_action(tx, job, action, "rejected", resp)
    raise PermanentError(f"POST /complaints: HTTP {resp.status} {resp.body.get('error', '')}".strip())


def _mark_complaint_sent(conn, job: Job, crm_id: str | None) -> None:
    with tenant_tx(conn, job.tenant_id):
        conn.execute("UPDATE complaints SET status = 'sent', crm_complaint_id = %s, updated_at = now() "
                     "WHERE recording_id = %s", [crm_id, job.recording_id])


# --- Booking --------------------------------------------------------------------------------------------

def _book(conn, job: Job, client: crm.CrmClient, ctx: dict, meeting: dict) -> None:
    for _ in range(MAX_SLOT_CONFLICTS):
        action = _open_action(conn, job, "booking")
        if action is None:
            existing = _find_call(client.get("/bookings", paced=False)["bookings"], job.recording_id)
            if existing:
                _mark_booked(conn, job, existing["slot_start"], existing["id"])
                return
            choice = crm.choose_slot(
                client.get("/slots", paced=True)["slots"], meeting["requested_kind"],
                meeting["requested_start"], meeting["requested_end"], ctx["recorded_at"],
            )
            if choice.slot_start is None:
                _mark_follow_up(conn, job, choice.reason)
                return
            action = _record_action(
                conn, job, "booking", crm.booking_key(job.tenant_id, job.recording_id, choice.slot_start),
                crm.booking_body(job.recording_id, choice, meeting["requested_start"], ctx["timezone"],
                                 crm.booking_note(caller_company(ctx["turns"]), meeting["topic"], meeting["format"],
                                                  choice.moved, ctx["complaint"] is not None)),
            )

        resp = _post(conn, job, client, "/bookings", action)
        if resp.status in (200, 201):
            with tenant_tx(conn, job.tenant_id) as tx:
                _finish_action(tx, job, action, "succeeded", resp)
            _mark_booked(conn, job, resp.body["slot_start"], resp.body.get("id"))
            return
        with tenant_tx(conn, job.tenant_id) as tx:
            _finish_action(tx, job, action, "rejected", resp)
        if resp.status != 409:
            raise PermanentError(f"POST /bookings: HTTP {resp.status} {resp.body.get('error', '')}".strip())
        # 409: someone else took the slot. The answer is definite, so a new slot (and key) is now safe.
    raise TransientError(f"slot taken {MAX_SLOT_CONFLICTS} times in a row")


def _mark_booked(conn, job: Job, slot_start: str, crm_id: str | None) -> None:
    with tenant_tx(conn, job.tenant_id):
        conn.execute(
            "UPDATE meetings SET status = 'booked', booked_slot_start = %s, crm_booking_id = %s, "
            "updated_at = now() WHERE recording_id = %s",
            [slot_start, crm_id, job.recording_id],
        )


def _mark_follow_up(conn, job: Job, reason: str) -> None:
    with tenant_tx(conn, job.tenant_id):
        conn.execute("UPDATE meetings SET status = 'needs_follow_up', follow_up_reason = %s, updated_at = now() "
                     "WHERE recording_id = %s", [reason, job.recording_id])


def _find_call(items: list[dict], call_id: str) -> dict | None:
    return next((i for i in items if i.get("call_id") == call_id), None)
