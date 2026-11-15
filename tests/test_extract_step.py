from datetime import datetime

import pytest
from psycopg.types.json import Jsonb

from app.db import tenant_tx
from app.extraction import Complaint, Day, Extraction, Meeting, Time
from app.queue import Job
from app.steps.extract import make_extract_step

JOB = Job("brightsmile", "rec_004", "extract", 0)


def meeting(status="requested", day="tomorrow", hour=18):
    return Meeting(
        status=status,
        topic="Oto servis; çağrıları karşılayan sesli asistan. İletişim: ali@ornek.com",
        format="online",
        day=Day(kind=day, weekday=None, next_week=False, day_of_month=None, month=None),
        time=Time(kind="exact" if hour is not None else "none", hour=hour, minute=0, end_hour=None, part_of_day=None),
        quote="Akşam altı olsun. Yarın.",
    )


class FakeExtractor:
    provider, model = "fake", "fake-1"

    def __init__(self, result):
        self.result, self.calls = result, 0

    def extract(self, dialogue):
        self.calls += 1
        return self.result


@pytest.fixture
def transcript(admin):
    def make(no_speech=False):
        with admin.transaction():
            admin.execute("TRUNCATE tenants, recordings, jobs CASCADE")
            admin.execute("INSERT INTO tenants (id, name, crm_base_url) VALUES ('brightsmile', 'L', 'x')")
            admin.execute("INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) "
                          "VALUES ('brightsmile', 'rec_004', 'x', '2026-10-30T11:10:59+03:00')")
            admin.execute("INSERT INTO transcripts (tenant_id, recording_id, provider, model, no_speech, turns, text) "
                          "VALUES ('brightsmile', 'rec_004', 'p', 'm', %s, %s, 'Caller: Yarın akşam altı.')",
                          [no_speech, Jsonb([])])
    return make


def rows(conn, table):
    with tenant_tx(conn, "brightsmile"):
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_meeting_is_stored_with_the_time_resolved_against_the_call(worker, transcript):
    transcript()
    make_extract_step(FakeExtractor(Extraction(meeting=meeting(), complaint=None)))(worker, JOB)
    [m] = rows(worker, "meetings")
    assert (m["status"], m["requested_kind"]) == ("pending", "exact")
    assert m["requested_start"] == datetime.fromisoformat("2026-10-31T18:00:00+03:00")  # the Saturday after the call
    # what the meeting is for, with anything shaped like personal data masked before it is stored
    assert (m["topic"], m["format"]) == ("Oto servis; çağrıları karşılayan sesli asistan. İletişim: [EMAIL]", "online")
    assert rows(worker, "complaints") == []


def test_withdrawn_request_needs_a_follow_up(worker, transcript):
    transcript()
    make_extract_step(FakeExtractor(Extraction(meeting=meeting(status="withdrawn"), complaint=None)))(worker, JOB)
    [m] = rows(worker, "meetings")
    assert m["status"] == "needs_follow_up" and "withdrew" in m["follow_up_reason"]


def test_unreadable_time_needs_a_follow_up_instead_of_a_guess(worker, transcript):
    transcript()
    bad = meeting(day="weekday")  # kind=weekday without a weekday
    make_extract_step(FakeExtractor(Extraction(meeting=bad, complaint=None)))(worker, JOB)
    [m] = rows(worker, "meetings")
    assert m["status"] == "needs_follow_up" and "could not be read" in m["follow_up_reason"]


def test_extracts_once_even_if_the_step_runs_again(worker, transcript):
    transcript()
    fake = FakeExtractor(Extraction(meeting=None, complaint=Complaint(summary="Fatura itirazı.")))
    step = make_extract_step(fake)
    step(worker, JOB)
    step(worker, JOB)
    assert fake.calls == 1 and len(rows(worker, "complaints")) == 1


def test_no_speech_calls_nothing_and_stores_an_empty_result(worker, transcript):
    transcript(no_speech=True)
    fake = FakeExtractor(Extraction(meeting=meeting(), complaint=None))
    make_extract_step(fake)(worker, JOB)
    assert fake.calls == 0
    [e] = rows(worker, "extractions")
    assert e["provider"] == "none" and e["result"] == {"meeting": None, "complaint": None}


def test_personal_data_in_a_summary_is_masked_before_it_is_stored(worker, transcript):
    transcript()
    leaky = Complaint(summary="İade TR33 0006 1005 1978 6457 8413 26 hesabına isteniyor.")
    make_extract_step(FakeExtractor(Extraction(meeting=None, complaint=leaky)))(worker, JOB)
    assert rows(worker, "complaints")[0]["summary"] == "İade [IBAN] hesabına isteniyor."
