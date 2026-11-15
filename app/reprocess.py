"""Put failed recordings back in the queue. They resume at the step they failed at; earlier steps,
whose results are stored, are not repeated.

Usage:
    python -m app.reprocess                      # every failed recording, all tenants
    python -m app.reprocess --tenant brightsmile       # one tenant's failed recordings
    python -m app.reprocess --tenant brightsmile --recording rec_004
"""

import argparse

from app.db import connect


def reprocess(conn, tenant_id: str | None = None, recording_id: str | None = None) -> list[dict]:
    if recording_id and not tenant_id:
        raise ValueError("--recording needs --tenant (recording ids are unique per tenant only)")
    return conn.execute(
        """
        UPDATE jobs SET status = 'queued', attempts = 0, run_at = now(), worker_id = NULL, waiting_since = NULL,
            updated_at = now()
        WHERE status = 'failed'
          AND (%(tenant)s::text IS NULL OR tenant_id = %(tenant)s)
          AND (%(recording)s::text IS NULL OR recording_id = %(recording)s)
        RETURNING tenant_id, recording_id, step, last_error
        """,
        {"tenant": tenant_id, "recording": recording_id},
    ).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-queue failed recordings from the step they failed at.")
    parser.add_argument("--tenant")
    parser.add_argument("--recording")
    args = parser.parse_args()

    with connect("worker") as conn:
        rows = reprocess(conn, args.tenant, args.recording)
    for r in rows:
        print(f"{r['tenant_id']}/{r['recording_id']}: re-queued at step '{r['step']}' (was: {r['last_error']})")
    print(f"{len(rows)} recording(s) re-queued")


if __name__ == "__main__":
    main()
