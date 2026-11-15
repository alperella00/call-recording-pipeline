"""The API, through HTTP, against the real database roles: a caller sees one tenant, the one its key belongs to."""

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import api_keys
from app.api import app

client = TestClient(app)

# Both tenants have a recording called rec_001 (ids are unique per tenant only), plus one of their own.
DATA = {
    "brightsmile": {"rec_001": "Arayan: Faturam iki kez kesildi.", "rec_002": "Arayan: Pazartesi 10'da görüşelim."},
    "shopnest": {"rec_001": "Arayan: ShopNest siparişim gelmedi.", "shopnest_002": None},   # shopnest_002 not masked yet
}


@pytest.fixture
def keys(admin):
    with admin.transaction():
        admin.execute("TRUNCATE tenants CASCADE")
        for tenant, recs in DATA.items():
            admin.execute("INSERT INTO tenants (id, name, crm_base_url) VALUES (%s, %s, 'http://crm')",
                          [tenant, tenant.title()])
            for i, (rec, text) in enumerate(recs.items()):
                admin.execute("INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) "
                              "VALUES (%s, %s, 'x', '2026-11-02T09:00:00+03:00'::timestamptz + %s * interval '1 min')",
                              [tenant, rec, i])
                done = text is not None
                admin.execute("INSERT INTO jobs (tenant_id, recording_id, step, status) VALUES (%s, %s, %s, %s)",
                              [tenant, rec, "done" if done else "mask", "done" if done else "retry_wait"])
                if done:
                    admin.execute(
                        "INSERT INTO transcripts (tenant_id, recording_id, provider, model, turns, text) "
                        "VALUES (%s, %s, 'test', 'test', %s, %s)",
                        [tenant, rec, json.dumps([{"speaker": "caller", "start": 0, "text": text, "marked": text}]),
                         text])
            first = next(iter(recs))
            admin.execute("INSERT INTO complaints (tenant_id, recording_id, status, summary) "
                          "VALUES (%s, %s, 'sent', %s)", [tenant, first, f"{tenant} complaint"])
            admin.execute("INSERT INTO meetings (tenant_id, recording_id, status, requested_kind, booked_slot_start) "
                          "VALUES (%s, %s, 'booked', 'exact', '2026-11-02T10:00:00+03:00')", [tenant, first])
    return {t: api_keys.create(admin, t) for t in DATA}


def get(path, key):
    return client.get(path, headers={"Authorization": f"Bearer {key}"} if key else {})


# --- Who is calling --------------------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, "cpk_not-a-real-key", ""])
def test_no_valid_key_no_data(keys, key):
    assert get("/recordings", key).status_code == 401


def test_revoked_key_stops_working(keys, admin):
    assert get("/recordings", keys["shopnest"]).status_code == 200
    assert api_keys.revoke(admin, keys["shopnest"])
    assert get("/recordings", keys["shopnest"]).status_code == 401


def test_no_endpoint_lets_the_caller_choose_a_tenant():
    """The tenant comes only from the key. A parameter named like a tenant on any route fails this test."""
    for path, ops in app.openapi()["paths"].items():
        assert "tenant" not in path
        for op in ops.values():
            for param in op.get("parameters", []):
                assert "tenant" not in param["name"].lower(), (path, param["name"])


# --- Each key sees its own tenant only -------------------------------------------------------------

@pytest.mark.parametrize("tenant", ["brightsmile", "shopnest"])
def test_lists_show_only_the_keys_tenant(keys, tenant):
    recs = get("/recordings", keys[tenant]).json()
    assert recs["tenant"] == tenant
    assert [r["id"] for r in recs["recordings"]] == list(DATA[tenant])
    assert [c["summary"] for c in get("/complaints", keys[tenant]).json()["complaints"]] == [f"{tenant} complaint"]
    assert len(get("/meetings", keys[tenant]).json()["meetings"]) == 1


def test_same_recording_id_gives_each_tenant_its_own_transcript(keys):
    assert get("/recordings/rec_001/transcript", keys["brightsmile"]).json()["text"] == DATA["brightsmile"]["rec_001"]
    assert get("/recordings/rec_001/transcript", keys["shopnest"]).json()["text"] == DATA["shopnest"]["rec_001"]


def test_another_tenants_recording_looks_like_it_does_not_exist(keys):
    other = get("/recordings/rec_002/transcript", keys["shopnest"])          # brightsmile's
    missing = get("/recordings/rec_999/transcript", keys["shopnest"])
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()


# --- What the caller sees ---------------------------------------------------------------------------

def test_status_and_times_in_the_tenants_time_zone(keys):
    recs = {r["id"]: r for r in get("/recordings", keys["shopnest"]).json()["recordings"]}
    assert (recs["rec_001"]["status"], recs["rec_001"]["step"]) == ("done", "done")
    assert (recs["shopnest_002"]["status"], recs["shopnest_002"]["step"]) == ("processing", "mask")
    assert recs["rec_001"]["recorded_at"] == "2026-11-02T09:00:00+03:00"
    assert get("/meetings", keys["shopnest"]).json()["meetings"][0]["booked_slot_start"] == "2026-11-02T10:00:00+03:00"


def test_transcript_not_ready_yet(keys):
    r = get("/recordings/shopnest_002/transcript", keys["shopnest"])
    assert r.status_code == 404 and "not transcribed" in r.json()["detail"]


# --- What the api's database role can't do, whatever the code does -------------------------------------

@pytest.mark.parametrize("table", ["api_keys", "raw_transcripts"])
def test_api_role_cannot_read_keys_or_raw_transcripts(api, keys, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute(f"SELECT * FROM {table}")


def test_key_lookup_answers_one_key_at_a_time(api, keys):
    lookup = "SELECT tenant_for_api_key(%s) AS t"
    assert api.execute(lookup, [keys["brightsmile"]]).fetchone()["t"] == "brightsmile"
    assert api.execute(lookup, ["cpk_guess"]).fetchone()["t"] is None


def test_key_is_stored_only_as_a_hash(admin, keys):
    stored = admin.execute("SELECT key_hash FROM api_keys WHERE tenant_id = 'brightsmile'").fetchone()["key_hash"]
    assert bytes(stored) == api_keys.key_hash(keys["brightsmile"]) and keys["brightsmile"].encode() not in bytes(stored)
