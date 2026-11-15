"""The CRM client (retries, rate limiting, exact bodies) and the slot choice, without a server."""

from datetime import datetime

import httpx
import pytest

from app import crm
from app.pipeline import TransientError


def client_answering(*answers, seen=None, sleeps=None):
    answers = list(answers)

    def handler(request):
        if seen is not None:
            seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return crm.CrmClient("http://crm", http=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_retries_server_errors_inside_the_step_then_succeeds():
    sleeps = []
    c = client_answering(httpx.Response(500), httpx.Response(429, headers={"Retry-After": "1"}),
                         httpx.Response(201, json={"id": "cp_1"}), sleeps=sleeps)
    resp = c.post("/complaints", "k", '{"call_id": "rec_003"}')
    assert (resp.status, resp.body["id"]) == (201, "cp_1")
    assert sleeps[1] >= 1   # Retry-After honoured


def test_gives_up_after_a_few_tries_and_hands_over_to_the_queue():
    c = client_answering(*[httpx.Response(429, headers={"Retry-After": "1"})] * crm.IN_STEP_TRIES)
    with pytest.raises(TransientError) as exc:
        c.post("/bookings", "k", "{}")
    assert exc.value.retry_after == 1


def test_dropped_connection_is_transient():
    c = client_answering(*[httpx.RemoteProtocolError("closed")] * crm.IN_STEP_TRIES)
    with pytest.raises(TransientError, match="RemoteProtocolError"):
        c.post("/bookings", "k", "{}")


def test_body_and_key_are_sent_exactly_as_stored():
    seen = []
    body = '{"call_id": "rec_003", "summary": "Fatura iki kez kesilmiş"}'
    client_answering(httpx.Response(201, json={}), seen=seen).post("/complaints", "v1|complaint|brightsmile|rec_003", body)
    assert seen[0].content == body.encode() and seen[0].headers["Idempotency-Key"] == "v1|complaint|brightsmile|rec_003"


def test_replayed_answer_is_recognised():
    c = client_answering(httpx.Response(201, json={"id": "bk_1"}, headers={"Idempotent-Replayed": "true"}))
    assert c.post("/bookings", "k", "{}").replayed


def test_pacing_spaces_requests_using_the_database_clock(worker, admin):
    admin.execute("TRUNCATE crm_pacing")
    sleeps = []
    pace = crm.db_pacer(worker, "http://crm", sleep=sleeps.append)
    pace()
    pace()
    assert len(sleeps) == 1 and 0.5 < sleeps[0] <= crm.SPACING_SECONDS


# --- Slot choice ------------------------------------------------------------------------------------------

SLOTS = [{"start": f"2026-11-02T{h}:00:00+03:00", "available": h not in (12, 13)} for h in range(10, 18)]
CALL = datetime.fromisoformat("2026-11-02T09:00:00+03:00")


def t(hour):
    return datetime.fromisoformat(f"2026-11-02T{hour:02d}:00:00+03:00")


def test_exact_time_free_is_booked_as_asked():
    assert crm.choose_slot(SLOTS, "exact", t(11), None, CALL).slot_start == "2026-11-02T11:00:00+03:00"


def test_exact_time_taken_gets_the_next_free_one():
    choice = crm.choose_slot(SLOTS, "exact", t(12), None, CALL)
    assert choice.slot_start == "2026-11-02T14:00:00+03:00" and "not available" in choice.reason


def test_window_takes_the_first_free_slot_inside_it_or_none():
    assert crm.choose_slot(SLOTS, "window", t(12), t(15), CALL).slot_start == "2026-11-02T14:00:00+03:00"
    assert crm.choose_slot(SLOTS, "window", t(12), t(14), CALL).slot_start is None


def test_no_day_and_no_time_is_not_booked():
    choice = crm.choose_slot(SLOTS, "none", None, None, CALL)
    assert choice.slot_start is None and "no day or time" in choice.reason


def test_slots_before_the_call_never_count():
    late_call = datetime.fromisoformat("2026-11-02T15:30:00+03:00")
    assert crm.choose_slot(SLOTS, "exact", t(10), None, late_call).slot_start == "2026-11-02T16:00:00+03:00"


def test_booking_note_is_the_same_text_for_the_same_call():
    args = ("Deniz Yazılım", "Çalışanlar için kurumsal diş taraması", "in_person", False, False)
    assert crm.booking_note(*args) == crm.booking_note(*args)
    assert crm.booking_note(*args) == ("Company: Deniz Yazılım\nTopic: Çalışanlar için kurumsal diş taraması\n"
                                       "Format: in person")


def test_only_a_different_time_is_marked_as_moved():
    assert not crm.choose_slot(SLOTS, "exact", t(11), None, CALL).moved
    assert crm.choose_slot(SLOTS, "exact", t(12), None, CALL).moved
    assert not crm.choose_slot(SLOTS, "window", t(12), t(15), CALL).moved   # inside what they asked for
