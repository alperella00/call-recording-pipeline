"""Database access.

There are three connection roles, each with its own DSN:

- admin  (DATABASE_URL_ADMIN):  migrations and provisioning only. Superuser, bypasses row-level security.
- worker (DATABASE_URL_WORKER): the pipeline. Can see every tenant's jobs, but tenant data only
                                 inside `tenant_tx`.
- api    (DATABASE_URL_API):    tenant-facing reads. Sees tenant data only inside `tenant_tx`.

Tenant data is only ever read or written through `tenant_tx`, which sets the tenant for one
transaction. Row-level security does the filtering; if the tenant is not set, queries return nothing.

Connections are in autocommit mode: a statement outside `with conn.transaction()` commits on its own, and
`tenant_tx` always opens a real transaction (never a savepoint inside one left open by accident).
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row


def connect(role: str) -> psycopg.Connection:
    dsn = os.environ[f"DATABASE_URL_{role.upper()}"]
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


@contextmanager
def tenant_tx(conn: psycopg.Connection, tenant_id: str) -> Iterator[psycopg.Connection]:
    """Run one transaction as `tenant_id`.

    `set_config(..., true)` is transaction-local (like SET LOCAL): the tenant is forgotten at commit or
    rollback, so it can never leak into the next transaction on the same connection.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        # Nested inside another transaction, the setting would outlive this block.
        raise RuntimeError("tenant_tx must not run inside another transaction")
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        yield conn
