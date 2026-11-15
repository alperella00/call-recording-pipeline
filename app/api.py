"""The tenant-facing API. A caller acts as exactly one tenant: the one its API key belongs to.

    GET /recordings                       the tenant's recordings and where each one is in the pipeline
    GET /recordings/{id}/transcript       a recording's masked transcript
    GET /meetings                         meeting requests: what was asked, what was booked, or why not
    GET /complaints                       complaints and whether they reached the CRM

Every request sends `Authorization: Bearer <key>` (create one with `python -m app.api_keys create <tenant>`).

How isolation holds:
- The api connects with its own database role, which can't bypass row-level security and can't read raw
  transcripts or API keys.
- The tenant comes only from the key, resolved by the database (`tenant_for_api_key`). No endpoint takes a
  tenant id as input, so there is nothing to tamper with.
- Every query runs inside `tenant_tx`, so row-level security filters it. A recording of another tenant is
  simply not there: 404, the same answer as for an id that doesn't exist.

Run: uvicorn app.api:app --host 0.0.0.0 --port 8000
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import connect, tenant_tx

app = FastAPI(title="callpipe", description="Recordings, masked transcripts, meetings and complaints of one tenant.")
bearer = HTTPBearer(auto_error=False)

PROCESSING = {"queued", "running", "retry_wait"}


class Tenant:
    def __init__(self, conn, tenant_id: str):
        self.conn = conn
        self.id = tenant_id

    def query(self, sql: str, params=None) -> tuple[list[dict], ZoneInfo]:
        """Run one read as this tenant. Also returns the tenant's time zone, to show times the way it sees them."""
        with tenant_tx(self.conn, self.id):
            tz = self.conn.execute("SELECT timezone FROM tenants").fetchone()["timezone"]
            rows = self.conn.execute(sql, params).fetchall()
        return rows, ZoneInfo(tz)


def current_tenant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Iterator[Tenant]:
    if creds is None:
        raise HTTPException(401, "missing API key", headers={"WWW-Authenticate": "Bearer"})
    with connect("api") as conn:
        tenant_id = conn.execute("SELECT tenant_for_api_key(%s) AS t", [creds.credentials]).fetchone()["t"]
        if tenant_id is None:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Bearer"})
        yield Tenant(conn, tenant_id)


TenantDep = Annotated[Tenant, Depends(current_tenant)]


def local(value: datetime | None, tz: ZoneInfo) -> str | None:
    return value.astimezone(tz).isoformat() if value else None


@app.get("/recordings")
def list_recordings(tenant: TenantDep) -> dict:
    rows, tz = tenant.query(
        """
        SELECT r.id, r.recorded_at, r.duplicate_of, j.step, j.status, j.attempts, j.last_error, j.updated_at
        FROM recordings r JOIN jobs j ON j.tenant_id = r.tenant_id AND j.recording_id = r.id
        ORDER BY r.recorded_at, r.id
        """
    )
    return {
        "tenant": tenant.id,
        "recordings": [
            {
                "id": r["id"],
                "recorded_at": local(r["recorded_at"], tz),
                # done | failed | processing; `step` says where it is (or where it failed)
                "status": "processing" if r["status"] in PROCESSING else r["status"],
                "step": r["step"],
                "detail": r["last_error"],
                "duplicate_of": r["duplicate_of"],
                "updated_at": local(r["updated_at"], tz),
            }
            for r in rows
        ],
    }


@app.get("/recordings/{recording_id}/transcript")
def get_transcript(recording_id: str, tenant: TenantDep) -> dict:
    rows, tz = tenant.query(
        """
        SELECT r.id, r.recorded_at, r.duplicate_of, t.no_speech, t.text, t.turns
        FROM recordings r
        LEFT JOIN transcripts t ON t.tenant_id = r.tenant_id AND t.recording_id = r.id
        WHERE r.id = %s
        """,
        [recording_id],
    )
    if not rows:
        raise HTTPException(404, "recording not found")
    r = rows[0]
    if r["text"] is None:
        detail = (f"same audio as {r['duplicate_of']}; see its transcript" if r["duplicate_of"]
                  else "not transcribed and masked yet")
        raise HTTPException(404, detail)
    return {
        "id": r["id"],
        "recorded_at": local(r["recorded_at"], tz),
        "no_speech": r["no_speech"],
        "text": r["text"],
        # `marked` is the same text with ⟨?⟩ after words speech-to-text was least sure about
        "turns": r["turns"],
    }


@app.get("/meetings")
def list_meetings(tenant: TenantDep) -> dict:
    rows, tz = tenant.query(
        """
        SELECT m.*, r.recorded_at FROM meetings m
        JOIN recordings r ON r.tenant_id = m.tenant_id AND r.id = m.recording_id
        ORDER BY r.recorded_at, m.recording_id
        """
    )
    return {
        "tenant": tenant.id,
        "meetings": [
            {
                "recording_id": m["recording_id"],
                "status": m["status"],   # booked | needs_follow_up | pending (not sent to the CRM yet)
                "topic": m["topic"],
                "format": m["format"],   # online | in_person | unknown
                "requested": {
                    "kind": m["requested_kind"],
                    "start": local(m["requested_start"], tz),
                    "end": local(m["requested_end"], tz),
                    "caller_said": m["requested_quote"],
                },
                "booked_slot_start": local(m["booked_slot_start"], tz),
                "crm_booking_id": m["crm_booking_id"],
                "follow_up_reason": m["follow_up_reason"],
            }
            for m in rows
        ],
    }


@app.get("/complaints")
def list_complaints(tenant: TenantDep) -> dict:
    rows, tz = tenant.query(
        """
        SELECT c.*, r.recorded_at FROM complaints c
        JOIN recordings r ON r.tenant_id = c.tenant_id AND r.id = c.recording_id
        ORDER BY r.recorded_at, c.recording_id
        """
    )
    return {
        "tenant": tenant.id,
        "complaints": [
            {
                "recording_id": c["recording_id"],
                "status": c["status"],   # sent | pending
                "summary": c["summary"],
                "crm_complaint_id": c["crm_complaint_id"],
            }
            for c in rows
        ],
    }
