"""The status command: finished means every recording is done or failed, per tenant, with failure reasons."""

from app.status import report, snapshot


def test_not_finished_while_a_recording_is_still_in_the_queue(worker, two_tenants, admin):
    text, finished = report(snapshot(worker))
    assert not finished and "processing 1" in text and text.endswith("still processing")


def test_finished_when_everything_is_done_or_failed_and_failures_are_listed(worker, two_tenants, admin):
    admin.execute("UPDATE jobs SET step = 'done', status = 'done' WHERE tenant_id = 'brightsmile'")
    admin.execute("UPDATE jobs SET step = 'transcribe', status = 'failed', "
                  "last_error = 'speech-to-text rejected the API key (HTTP 401, invalid_api_key)' "
                  "WHERE tenant_id = 'shopnest'")
    with admin.transaction():
        admin.execute("INSERT INTO meetings (tenant_id, recording_id, status, requested_kind) "
                      "VALUES ('brightsmile', 'rec_001', 'booked', 'exact')")
    text, finished = report(snapshot(worker))
    assert finished and text.endswith("finished")
    assert "brightsmile: 1 recordings | done 1, failed 0, processing 0 | meetings: booked 1" in text
    assert "failed shopnest_001 at transcribe: speech-to-text rejected the API key" in text


def test_an_empty_database_is_not_finished(worker, admin):
    admin.execute("TRUNCATE tenants, recordings, jobs CASCADE")
    assert report(snapshot(worker)) == ("no recordings yet\nstill processing", False)
