"""Apply SQL migrations in order. Safe to run many times and from several containers at once.

1. Create (or update the password of) the restricted login roles used by the api and the worker.
   Passwords come from the environment, never from the SQL files.
2. Apply every `migrations/NNN_*.sql` file not yet recorded in `schema_migrations`,
   each in its own transaction.

Usage: python -m app.migrate
"""

import os
from pathlib import Path

from psycopg import sql

from app.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Login roles for the running services. They don't own any table, are not superusers and can't
# bypass row-level security.
APP_ROLES = {
    "app_api": "APP_API_DB_PASSWORD",
    "app_worker": "APP_WORKER_DB_PASSWORD",
}


def ensure_roles(conn) -> None:
    for role, password_env in APP_ROLES.items():
        password = os.environ[password_env]
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(
                "{} ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}"
            ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(password))
        )


def main() -> None:
    with connect("admin") as conn:
        # Only one migrator at a time, even if several containers start together.
        conn.execute("SELECT pg_advisory_lock(hashtext('schema_migrations'))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        ensure_roles(conn)
        conn.commit()

        applied = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", [path.name])
            print(f"applied {path.name}", flush=True)
        print("migrations up to date", flush=True)


if __name__ == "__main__":
    main()
