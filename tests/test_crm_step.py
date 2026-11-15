"""The CRM step against the CRM mock (crm_mock/server.py), run in-process with CHAOS=0.

Failures are injected at chosen moments by wrapping the client, so each scenario is deterministic.
"""

import importlib.util
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import crm
from app.db import connect, tenant_tx
from app.pipeline import Deferred, TransientError
from app.queue import Job
from app.steps import crm as crm_step_module
from app.steps.crm import make_crm_step

CRM_DIR = Path(__file__).resolve().parent.parent / "crm_mock"


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(crm, "SPACING_SECONDS", 0)  # CHAOS=0 has no rate limit; pacing has its own test


@pytest.fixture
def crm_server():
    spec = importlib.util.spec_from_file_location(f"crm_mock_{uuid.uuid4().hex[:6]}", CRM_DIR / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mock = mod.Crm(json.loads((CRM_DIR / "calendar.json").read_text()), chaos=False)
    server = mod.make_server(mock, quiet=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", state=mock.state)
    server.shutdown()
    server.server_close()


@pytest.fixture
def calls(admin, crm_server):
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs, crm_pacing CASCADE")
        admin.execute("INSERT INTO tenants (id, name, crm_base_url, min_notice_hours) VALUES ('brightsmile', 'L', %s, 4)",
                      [crm_server.url])

    def add(rec, recorded_at, kind=None, start=None, meeting_status="pending", complaint=None,
            step="crm", job_status="queued", duplicate_of=None, meeting_age_seconds=0, waiting_seconds=None):
        with admin.transaction():
            admin.execute("INSERT INTO recordings (tenant_id, id, audio_path, recorded_at, duplicate_of) "
                          "VALUES ('brightsmile', %s, 'x', %s, %s)", [rec, recorded_at, duplicate_of])
            admin.execute("INSERT INTO jobs (tenant_id, recording_id, step, status, waiting_since) "
                          "VALUES ('brightsmile', %s, %s, %s, now() - make_interval(secs => %s))",
                          [rec, step, job_status, waiting_seconds])
            if kind:
                admin.execute(
                    "INSERT INTO meetings (tenant_id, recording_id, status, requested_kind, requested_start, "
                    "requested_quote, created_at) VALUES ('brightsmile', %s, %s, %s, %s, 'yarın akşam altı', "
                    "now() - make_interval(secs => %s))",
                    [rec, meeting_status, kind, start, meeting_age_seconds])
            if complaint:
                admin.execute("INSERT INTO complaints (tenant_id, recording_id, status, summary) "
                              "VALUES ('brightsmile', %s, 'pending', %s)", [rec, complaint])
    return add


def run(conn, rec, client_factory=crm_step_module.default_client):
    make_crm_step(client_factory)(conn, Job("brightsmile", rec, "crm", 0))


def in_crm(crm_server, what, rec):
    return [x for x in crm_server.state[what] if x["call_id"] == rec]


def meeting(conn, rec):
    with tenant_tx(conn, "brightsmile"):
        return conn.execute("SELECT * FROM meetings WHERE recording_id = %s", [rec]).fetchone()


def actions(conn, rec):
    with tenant_tx(conn, "brightsmile"):
        return conn.execute("SELECT * FROM crm_actions WHERE recording_id = %s ORDER BY created_at",
                            [rec]).fetchall()


def ts(s):
    return datetime.fromisoformat(s)


# --- A morning of calls, booked in call order --------------------------------------------------------------
# The calendar (crm_mock/calendar.json): weekdays 09-17 without 12:00, from Fri 13 Nov; Fri 13:00,
# Mon 16 09:00 and 13:00 are taken before any call. The calls are all on Friday 13 Nov.

EXPECTED = [  # call, recorded at, kind, requested start, booked slot (None = needs follow-up)
    # Saturday is closed; Monday 09:00 is taken: the first free slot after the request is Monday 10:00.
    ("rec_002", "2026-11-13T09:40:00+03:00", "exact", "2026-11-14T11:00:00+03:00", "2026-11-16T10:00:00+03:00"),
    # Monday 09:00 is taken, 10:00 went to the earlier call.
    ("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-16T09:00:00+03:00", "2026-11-16T11:00:00+03:00"),
    # The same afternoon: 13:00 is taken.
    ("rec_005", "2026-11-13T10:30:00+03:00", "exact", "2026-11-13T13:00:00+03:00", "2026-11-13T14:00:00+03:00"),
    # The caller withdrew the request (the extract step already marked it for follow-up).
    ("rec_007", "2026-11-13T10:50:00+03:00", "exact", "2026-11-16T10:00:00+03:00", None),
    # Wants a meeting, never says when.
    ("rec_008", "2026-11-13T11:05:00+03:00", "none", None, None),
    # Monday 10:00 and 11:00 went to earlier calls, 13:00 was taken before.
    ("rec_009", "2026-11-13T11:20:00+03:00", "exact", "2026-11-16T10:00:00+03:00", "2026-11-16T14:00:00+03:00"),
    # Friday 14:00 went to an earlier call.
    ("rec_011", "2026-11-13T11:45:00+03:00", "exact", "2026-11-13T14:00:00+03:00", "2026-11-13T15:00:00+03:00"),
]


def test_calls_get_the_expected_slots_in_call_order_whatever_order_workers_run_them(worker, calls, crm_server):
    for rec, at, kind, start, booked in EXPECTED:
        # rec_007 was withdrawn (follow-up from the extract step); the rest arrive pending. The requests were
        # extracted a day ago (resumed after an outage or a reprocess): the order must hold anyway.
        calls(rec, at, kind, start, meeting_status="needs_follow_up" if rec == "rec_007" else "pending",
              meeting_age_seconds=86400)

    # Workers pick the calls up latest first. Later calls must wait for earlier ones.
    todo = [rec for rec, *_ in reversed(EXPECTED)]
    deferred = 0
    for _ in range(len(todo) + 1):
        for rec in list(todo):
            try:
                run(worker, rec)
                todo.remove(rec)
            except Deferred:
                deferred += 1
    assert todo == [] and deferred > 0

    for rec, _, _, _, booked in EXPECTED:
        m = meeting(worker, rec)
        if booked is None:
            assert m["status"] == "needs_follow_up" and in_crm(crm_server, "bookings", rec) == []
        else:
            assert (m["status"], m["booked_slot_start"]) == ("booked", ts(booked)), rec
            [b] = in_crm(crm_server, "bookings", rec)
            assert (b["slot_start"], b["id"]) == (booked, m["crm_booking_id"])


# --- A worker dies while its POST is still in flight -------------------------------------------------------

class Killed(BaseException):
    """Stands in for SIGKILL: not caught by any `except Exception`."""


class DiesBeforeTheAnswer(crm.CrmClient):
    def post(self, path, idempotency_key, body):
        raise Killed()


class DelayedRequestLands(crm.CrmClient):
    """The dead worker's POST was delayed (CHAOS has 8-second responses) and reaches the CRM at the worst
    moment: right after this worker's first look at the CRM's bookings, or just before its own POST."""

    def __init__(self, base_url, land):
        super().__init__(base_url)
        self.land, self.landed = land, False

    def _land_once(self):
        if not self.landed:
            self.landed = True
            self.land()

    def get(self, path, paced):
        result = super().get(path, paced)
        if path == "/bookings":
            self._land_once()
        return result

    def post(self, path, idempotency_key, body):
        self._land_once()
        return super().post(path, idempotency_key, body)


def test_worker_killed_during_post_then_delayed_request_lands_gives_one_booking(calls, crm_server, admin):
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00")

    with connect("worker") as a:
        with pytest.raises(Killed):
            run(a, "rec_004", lambda conn, url: DiesBeforeTheAnswer(url))
    [sent] = actions(admin, "rec_004")   # A wrote its request down before sending it
    assert sent["status"] == "pending"

    def a_request_lands():
        httpx.post(crm_server.url + "/bookings", content=sent["request_body"].encode(),
                   headers={"Idempotency-Key": sent["idempotency_key"], "Content-Type": "application/json"})

    with connect("worker") as b:
        run(b, "rec_004", lambda conn, url: DelayedRequestLands(url, a_request_lands))
        m = meeting(b, "rec_004")

    [booking] = in_crm(crm_server, "bookings", "rec_004")
    assert booking["slot_start"] == "2026-11-16T10:00:00+03:00"
    assert (m["status"], m["crm_booking_id"]) == ("booked", booking["id"])
    assert [x["status"] for x in actions(admin, "rec_004")] == ["succeeded"]


class LosesTheAnswer(crm.CrmClient):
    """CHAOS 'lost response': the write is committed, then the connection closes."""

    def post(self, path, idempotency_key, body):
        super().post(path, idempotency_key, body)
        raise TransientError("POST: RemoteProtocolError")


def test_lost_response_is_resent_with_the_same_key_and_creates_nothing_new(worker, calls, crm_server, admin):
    calls("rec_003", "2026-11-13T09:55:00+03:00", complaint="Aynı tedavi için iki kez ödeme alınmış")
    with pytest.raises(TransientError):
        run(worker, "rec_003", lambda conn, url: LosesTheAnswer(url))
    run(worker, "rec_003")

    [complaint] = in_crm(crm_server, "complaints", "rec_003")
    [action] = actions(admin, "rec_003")
    assert (action["status"], action["attempts"], action["crm_id"]) == ("succeeded", 2, complaint["id"])


# --- Other outcomes ------------------------------------------------------------------------------------

def test_slot_taken_between_looking_and_booking_moves_to_the_next_slot(worker, calls, crm_server, admin):
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00")

    class SomeoneIsFaster(crm.CrmClient):
        def post(self, path, idempotency_key, body):
            if not crm_server.state["bookings"]:
                httpx.post(crm_server.url + "/bookings", headers={"Idempotency-Key": "other"},
                           json={"call_id": "other", "slot_start": "2026-11-16T10:00:00+03:00"})
            return super().post(path, idempotency_key, body)

    run(worker, "rec_004", lambda conn, url: SomeoneIsFaster(url))

    assert meeting(worker, "rec_004")["booked_slot_start"] == ts("2026-11-16T11:00:00+03:00")
    assert [(a["status"], a["http_status"]) for a in actions(admin, "rec_004")] == [("rejected", 409),
                                                                                  ("succeeded", 201)]


def test_complaint_and_booking_are_sent_once_and_a_rerun_sends_nothing(worker, calls, crm_server):
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00", complaint="Randevu saatinde bekletildi")
    run(worker, "rec_004")
    run(worker, "rec_004")
    assert len(in_crm(crm_server, "bookings", "rec_004")) == 1
    assert len(in_crm(crm_server, "complaints", "rec_004")) == 1


def test_after_a_database_reset_existing_crm_entries_are_adopted(worker, calls, crm_server, admin):
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00", complaint="Randevu saatinde bekletildi")
    run(worker, "rec_004")
    first = meeting(worker, "rec_004")

    # Our database forgets what it sent; the CRM still has it.
    admin.execute("DELETE FROM crm_actions")
    admin.execute("UPDATE meetings SET status = 'pending', booked_slot_start = NULL, crm_booking_id = NULL")
    admin.execute("UPDATE complaints SET status = 'pending', crm_complaint_id = NULL")
    run(worker, "rec_004")

    assert len(in_crm(crm_server, "bookings", "rec_004")) == 1
    assert len(in_crm(crm_server, "complaints", "rec_004")) == 1
    assert meeting(worker, "rec_004")["crm_booking_id"] == first["crm_booking_id"]


def test_no_slot_after_the_requested_time_needs_follow_up(worker, calls, crm_server):
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2027-01-04T10:00:00+03:00")
    run(worker, "rec_004")
    m = meeting(worker, "rec_004")
    assert m["status"] == "needs_follow_up" and "no available slot" in m["follow_up_reason"]
    assert crm_server.state["bookings"] == []


def test_a_duplicate_upload_sends_nothing(worker, calls, crm_server):
    calls("rec_001", "2026-11-13T09:05:00+03:00")
    calls("rec_021", "2026-11-13T12:30:00+03:00", complaint="Aynı arama", duplicate_of="rec_001")
    run(worker, "rec_021")
    assert crm_server.state["complaints"] == []


# --- Waiting for earlier calls, and the lane -----------------------------------------------------------------

def test_waits_for_an_earlier_call_that_is_not_extracted_yet(worker, calls, crm_server):
    calls("rec_002", "2026-11-13T09:40:00+03:00", step="extract")
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00", complaint="Randevu saatinde bekletildi")
    with pytest.raises(Deferred, match="rec_002"):
        run(worker, "rec_004")
    assert len(in_crm(crm_server, "complaints", "rec_004")) == 1   # complaints don't compete for slots
    assert crm_server.state["bookings"] == []


def test_stops_waiting_after_the_limit(worker, calls, crm_server):
    calls("rec_002", "2026-11-13T09:40:00+03:00", step="extract")
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00",
          waiting_seconds=crm_step_module.WAIT_FOR_EARLIER_SECONDS + 1)
    run(worker, "rec_004")
    assert meeting(worker, "rec_004")["status"] == "booked"


def test_does_not_wait_for_a_failed_earlier_call(worker, calls, crm_server):
    calls("rec_002", "2026-11-13T09:40:00+03:00", "exact", "2026-11-14T16:00:00+03:00", job_status="failed")
    calls("rec_004", "2026-11-13T10:15:00+03:00", "exact", "2026-11-14T18:00:00+03:00")
    run(worker, "rec_004")
    assert meeting(worker, "rec_004")["booked_slot_start"] == ts("2026-11-16T10:00:00+03:00")


def test_waits_while_another_worker_holds_the_crm_lane(worker, calls, crm_server):
    calls("rec_003", "2026-11-13T09:55:00+03:00", complaint="Ödeme sorunu")
    with connect("worker") as other:
        other.execute("SELECT pg_advisory_lock(%s, hashtext(%s))",
                      [crm_step_module.LANE_LOCK_SPACE, crm_server.url])
        with pytest.raises(Deferred, match="another worker"):
            run(worker, "rec_003")
    run(worker, "rec_003")   # the other worker's connection closed: the lane is free
    assert len(in_crm(crm_server, "complaints", "rec_003")) == 1


def test_lane_is_released_after_the_step_even_when_it_fails(worker, calls, crm_server):
    calls("rec_003", "2026-11-13T09:55:00+03:00", complaint="Ödeme sorunu")
    with pytest.raises(TransientError):
        run(worker, "rec_003", lambda conn, url: LosesTheAnswer(url))
    held = worker.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' "
                          "AND pid = pg_backend_pid()").fetchone()["n"]
    assert held == 0


# --- What the team reads in the CRM ----------------------------------------------------------------------

def _describe(admin, rec, topic, fmt, turns=()):
    with admin.transaction():
        admin.execute("UPDATE meetings SET topic = %s, format = %s WHERE recording_id = %s", [topic, fmt, rec])
        admin.execute("INSERT INTO transcripts (tenant_id, recording_id, provider, model, turns, text) "
                      "VALUES ('brightsmile', %s, 't', 't', %s, '')", [rec, json.dumps(list(turns))])


def test_booking_note_says_who_what_and_how(worker, admin, calls, crm_server):
    calls("rec_014", "2026-11-13T11:30:00+03:00", "exact", "2026-11-16T10:00:00+03:00", complaint="Ödeme sorunu")
    _describe(admin, "rec_014", "Çalışanlar için kurumsal diş taraması", "online", [
        {"speaker": "agent", "start": 0, "text": "Teşekkürler [NAME] Bey. Şirketinizin adını öğrenebilir miyim?"},
        {"speaker": "caller", "start": 1, "text": "Ceylan Lojistik."},
    ])
    run(worker, "rec_014")
    [b] = in_crm(crm_server, "bookings", "rec_014")
    assert b["note"] == ("Company: Ceylan Lojistik\nTopic: Çalışanlar için kurumsal diş taraması\n"
                         "Format: online\nThe same call also raised a complaint (see complaints, same call_id).")


def test_booking_note_asks_to_confirm_a_moved_time(worker, admin, calls, crm_server):
    # Saturday 16:00 is outside the calendar: the caller gets Monday 10:00 and has to be told.
    calls("rec_002", "2026-11-13T09:40:00+03:00", "exact", "2026-11-14T16:00:00+03:00")
    _describe(admin, "rec_002", "", "unknown")
    run(worker, "rec_002")
    [b] = in_crm(crm_server, "bookings", "rec_002")
    assert b["note"].startswith("Company: not given in the call\nTopic: not extracted\nFormat: not discussed\n")
    assert "Confirm this one with them" in b["note"] and "complaint" not in b["note"]
