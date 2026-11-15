"""Test fixtures: every test session gets its own throwaway database with all migrations applied.

Run with: docker compose run --rm tests
"""

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import migrate
from app.db import connect

ROLES = ("admin", "worker", "api")


def _with_dbname(dsn: str, dbname: str) -> str:
    return make_conninfo(dsn, dbname=dbname)


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create a fresh database, point every role's DSN at it, migrate, and drop it afterwards."""
    admin_dsn = os.environ["DATABASE_URL_ADMIN"]
    name = f"test_{uuid.uuid4().hex[:8]}"
    original = {role: os.environ[f"DATABASE_URL_{role.upper()}"] for role in ROLES}

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    for role, dsn in original.items():
        os.environ[f"DATABASE_URL_{role.upper()}"] = _with_dbname(dsn, name)

    migrate.main()
    yield name

    os.environ.update({f"DATABASE_URL_{r.upper()}": dsn for r, dsn in original.items()})
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def admin():
    with connect("admin") as conn:
        yield conn


@pytest.fixture
def worker():
    with connect("worker") as conn:
        yield conn


@pytest.fixture
def api():
    with connect("api") as conn:
        yield conn


@pytest.fixture
def two_tenants(admin):
    """Two tenants, one recording and one job each. Cleaned before every test."""
    with admin.transaction():
        admin.execute("TRUNCATE tenants, recordings, jobs CASCADE")
        for tenant, rec in (("brightsmile", "rec_001"), ("shopnest", "shopnest_001")):
            admin.execute(
                "INSERT INTO tenants (id, name, crm_base_url) VALUES (%s, %s, %s)",
                [tenant, tenant.title(), f"http://crm-{tenant}:8080"],
            )
            admin.execute(
                "INSERT INTO recordings (tenant_id, id, audio_path, recorded_at) VALUES (%s, %s, 'x', now())",
                [tenant, rec],
            )
            admin.execute("INSERT INTO jobs (tenant_id, recording_id) VALUES (%s, %s)", [tenant, rec])
    return {"brightsmile": "rec_001", "shopnest": "shopnest_001"}
