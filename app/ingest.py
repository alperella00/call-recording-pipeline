"""Register tenants and recordings, and queue every recording for processing.

Safe to run any number of times: tenants are upserted, recordings and jobs that already exist are left
alone, so re-running never duplicates or resets work.

Every recording in a manifest gets a row and a job, even when its audio file is missing: that job is
created as failed with the reason, so nothing is silently skipped. If the file shows up later,
`reprocess` picks it up.

Usage: python -m app.ingest   (manifests listed in MANIFESTS, comma-separated)
"""

import hashlib
import json
import os
from pathlib import Path

from app.db import connect, tenant_tx

TENANTS_FILE = Path(os.environ.get("TENANTS_FILE", Path(__file__).resolve().parent.parent / "config/tenants.json"))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_audio(manifest_dir: Path, relative: str) -> Path:
    """Resolve a manifest file entry, refusing paths that escape the manifest's folder."""
    path = (manifest_dir / relative).resolve()
    if not path.is_relative_to(manifest_dir.resolve()):
        raise ValueError(f"audio path outside the manifest folder: {relative!r}")
    return path


def upsert_tenants(admin_conn) -> set[str]:
    """Tenants are provisioning data, written with the admin role."""
    tenants = json.loads(TENANTS_FILE.read_text())["tenants"]
    with admin_conn.transaction():
        for t in tenants:
            admin_conn.execute(
                """
                INSERT INTO tenants (id, name, timezone, assistant_name, crm_base_url, min_notice_hours)
                VALUES (%(id)s, %(name)s, %(timezone)s, %(assistant_name)s, %(crm_base_url)s, %(min_notice_hours)s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, timezone = EXCLUDED.timezone,
                    assistant_name = EXCLUDED.assistant_name, crm_base_url = EXCLUDED.crm_base_url,
                    min_notice_hours = EXCLUDED.min_notice_hours
                """,
                t,
            )
    return {t["id"] for t in tenants}


def ingest_recording(conn, manifest_dir: Path, rec: dict) -> str:
    """Insert one recording and its job, as that recording's tenant. Returns what happened."""
    tenant_id = rec["tenant_id"]
    audio_path = resolve_audio(manifest_dir, rec["file"])
    sha = sha256_of(audio_path) if audio_path.is_file() else None

    with tenant_tx(conn, tenant_id):
        inserted = conn.execute(
            """
            INSERT INTO recordings (tenant_id, id, audio_path, audio_sha256, recorded_at, language)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, id) DO NOTHING
            RETURNING id
            """,
            [tenant_id, rec["id"], str(audio_path), sha, rec["recorded_at"], rec.get("language", "tr")],
        ).fetchone()
        if not inserted:
            return "already registered"

        # The same audio under several ids is one call uploaded several times. The earliest call is the
        # original, whatever order the manifest lists them in; every other copy points to it.
        original = None
        if sha:
            original = conn.execute(
                "SELECT id FROM recordings WHERE audio_sha256 = %s ORDER BY recorded_at, id LIMIT 1",
                [sha],
            ).fetchone()
            conn.execute(
                """
                UPDATE recordings SET duplicate_of = %s
                WHERE audio_sha256 = %s AND id <> %s AND duplicate_of IS DISTINCT FROM %s
                """,
                [original["id"], sha, original["id"], original["id"]],
            )
            if original["id"] == rec["id"]:
                original = None

        if sha is None:
            conn.execute(
                """
                INSERT INTO jobs (tenant_id, recording_id, status, last_error)
                VALUES (%s, %s, 'failed', 'audio file not found')
                """,
                [tenant_id, rec["id"]],
            )
            return "registered, FAILED: audio file not found"

        conn.execute("INSERT INTO jobs (tenant_id, recording_id) VALUES (%s, %s)", [tenant_id, rec["id"]])
        return f"queued (duplicate of {original['id']})" if original else "queued"


def main() -> None:
    manifests = [Path(p) for p in os.environ["MANIFESTS"].split(",") if p.strip()]

    with connect("admin") as admin_conn:
        known_tenants = upsert_tenants(admin_conn)

    with connect("worker") as conn:
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text())
            for rec in manifest["recordings"]:
                if rec["tenant_id"] not in known_tenants:
                    raise SystemExit(f"{manifest_path}: unknown tenant {rec['tenant_id']!r} for {rec['id']}")
                outcome = ingest_recording(conn, manifest_path.parent, rec)
                print(f"{rec['tenant_id']}/{rec['id']}: {outcome}", flush=True)


if __name__ == "__main__":
    main()
