"""Create and revoke API keys. Provisioning, so it uses the admin connection.

Usage:
    python -m app.api_keys create brightsmile [--label "demo"]   # prints the key once; only its hash is stored
    python -m app.api_keys revoke <key>
"""

import argparse
import hashlib
import secrets
import sys

from app.db import connect

PREFIX = "cpk_"   # recognisable in logs and secret scanners


def key_hash(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def create(admin_conn, tenant_id: str, label: str = "") -> str:
    if not admin_conn.execute("SELECT 1 FROM tenants WHERE id = %s", [tenant_id]).fetchone():
        raise ValueError(f"unknown tenant {tenant_id!r}")
    key = PREFIX + secrets.token_urlsafe(32)
    admin_conn.execute("INSERT INTO api_keys (key_hash, tenant_id, label) VALUES (%s, %s, %s)",
                       [key_hash(key), tenant_id, label])
    return key


def revoke(admin_conn, key: str) -> bool:
    row = admin_conn.execute("UPDATE api_keys SET revoked_at = now() WHERE key_hash = %s AND revoked_at IS NULL "
                             "RETURNING 1", [key_hash(key)]).fetchone()
    return row is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", help="create a key for a tenant and print it")
    c.add_argument("tenant")
    c.add_argument("--label", default="")
    r = sub.add_parser("revoke", help="revoke a key")
    r.add_argument("key")
    args = parser.parse_args()

    with connect("admin") as conn:
        if args.command == "create":
            try:
                print(create(conn, args.tenant, args.label))
            except ValueError as exc:
                sys.exit(str(exc))
        else:
            sys.exit(0 if revoke(conn, args.key) else "no such active key")


if __name__ == "__main__":
    main()
