"""Tenant isolation is enforced by the database, so these tests talk to Postgres as the real roles."""

import psycopg
import pytest

from app.db import tenant_tx

TENANT_TABLE_QUERY = """
    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
           (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) AS policies
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND (c.relname = 'tenants' OR EXISTS (
            SELECT 1 FROM information_schema.columns col
            WHERE col.table_schema = 'public' AND col.table_name = c.relname AND col.column_name = 'tenant_id'))
"""


def ids(conn, table="recordings"):
    return sorted(r["id"] if "id" in r else r["recording_id"] for r in conn.execute(f"SELECT * FROM {table}"))


# --- The safety net: a new table without RLS fails this test ----------------------------------------

def test_every_tenant_table_has_forced_rls_and_a_policy(admin):
    tables = admin.execute(TENANT_TABLE_QUERY).fetchall()
    assert {"tenants", "recordings", "jobs"} <= {t["relname"] for t in tables}
    for t in tables:
        assert t["relrowsecurity"], f"{t['relname']}: row-level security not enabled"
        assert t["relforcerowsecurity"], f"{t['relname']}: row-level security not forced"
        assert t["policies"] > 0, f"{t['relname']}: no policy"


def test_app_roles_cannot_bypass_rls(admin):
    for role in ("app_api", "app_worker"):
        r = admin.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", [role]).fetchone()
        assert r == {"rolsuper": False, "rolbypassrls": False}, role
        owned = admin.execute("SELECT count(*) AS n FROM pg_tables WHERE tableowner = %s", [role]).fetchone()
        assert owned["n"] == 0, f"{role} owns tables, so policies would not apply to it"


# --- Reading ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["api", "worker"])
def test_no_tenant_set_sees_nothing(request, two_tenants, role):
    conn = request.getfixturevalue(role)
    assert ids(conn) == []


@pytest.mark.parametrize("role", ["api", "worker"])
def test_tenant_sees_only_its_own_rows(request, two_tenants, role):
    conn = request.getfixturevalue(role)
    with tenant_tx(conn, "shopnest"):
        assert ids(conn) == ["shopnest_001"]
        assert ids(conn, "tenants") == ["shopnest"]
    with tenant_tx(conn, "brightsmile"):
        assert ids(conn) == ["rec_001"]


def test_asking_for_another_tenant_explicitly_returns_nothing(api, two_tenants):
    with tenant_tx(api, "shopnest"):
        rows = api.execute("SELECT * FROM recordings WHERE tenant_id = 'brightsmile'").fetchall()
    assert rows == []


def test_tenant_setting_does_not_outlive_the_transaction(api, two_tenants):
    with tenant_tx(api, "shopnest"):
        pass
    assert ids(api) == []


def test_api_sees_only_its_tenants_jobs_but_worker_sees_the_whole_queue(api, worker, two_tenants):
    with tenant_tx(api, "shopnest"):
        assert ids(api, "jobs") == ["shopnest_001"]
    assert ids(worker, "jobs") == ["rec_001", "shopnest_001"]


def test_tenant_tx_requires_a_tenant(api):
    with pytest.raises(ValueError):
        with tenant_tx(api, ""):
            pass


# --- Writing ---------------------------------------------------------------------------------------

def test_worker_cannot_write_a_row_for_another_tenant(worker, two_tenants):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with tenant_tx(worker, "shopnest"):
            worker.execute(
                "INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) VALUES ('brightsmile', 'x', 'x', now())"
            )


def test_worker_cannot_move_a_row_to_another_tenant(worker, two_tenants):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(worker, "shopnest"):
            worker.execute("UPDATE recordings SET tenant_id = 'brightsmile' WHERE id = 'shopnest_001'")


def test_api_is_read_only(api, two_tenants):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(api, "shopnest"):
            api.execute("UPDATE jobs SET status = 'done'")
