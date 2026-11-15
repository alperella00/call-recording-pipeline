"""Where every recording is, and whether processing has finished.

Usage:
    python -m app.status                 # summary per tenant, then the failed recordings with their reason
    python -m app.status --wait 600      # keep checking until finished (or give up after 600 s)

Exit code: 0 when every recording is finished (done or failed), 1 while some are still being processed.
Failed recordings are listed with their step and reason; `python -m app.reprocess` puts them back in the queue.

Uses the worker role: it can read the whole queue (ids, steps and statuses only), and reads each tenant's
meetings and complaints as that tenant.
"""

import argparse
import sys
import time

from app.db import connect, tenant_tx

PROCESSING = ("queued", "running", "retry_wait")


def snapshot(conn) -> dict:
    jobs = conn.execute(
        "SELECT tenant_id, recording_id, step, status, last_error FROM jobs ORDER BY tenant_id, recording_id"
    ).fetchall()
    tenants = {}
    for j in jobs:
        t = tenants.setdefault(j["tenant_id"], {"done": 0, "failed": [], "processing": 0})
        if j["status"] == "done":
            t["done"] += 1
        elif j["status"] == "failed":
            t["failed"].append(j)
        else:
            t["processing"] += 1
    for tenant_id, t in tenants.items():
        with tenant_tx(conn, tenant_id):
            t["meetings"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM meetings GROUP BY status")}
            t["complaints"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM complaints GROUP BY status")}
    return tenants


def report(tenants: dict) -> tuple[str, bool]:
    lines, finished = [], True
    for tenant_id, t in tenants.items():
        total = t["done"] + len(t["failed"]) + t["processing"]
        finished &= t["processing"] == 0
        m, c = t["meetings"], t["complaints"]
        lines.append(
            f"{tenant_id}: {total} recordings | done {t['done']}, failed {len(t['failed'])}, "
            f"processing {t['processing']} | meetings: booked {m.get('booked', 0)}, "
            f"needs follow-up {m.get('needs_follow_up', 0)}, pending {m.get('pending', 0)} | "
            f"complaints: sent {c.get('sent', 0)}, pending {c.get('pending', 0)}"
        )
        for j in t["failed"]:
            lines.append(f"  failed {j['recording_id']} at {j['step']}: {j['last_error']}")
    if not tenants:
        lines.append("no recordings yet")
        finished = False
    lines.append("finished" if finished else "still processing")
    return "\n".join(lines), finished


def main() -> None:
    parser = argparse.ArgumentParser(description="Show processing status; exit 0 when everything is finished.")
    parser.add_argument("--wait", type=float, metavar="SECONDS", help="poll until finished, at most this long")
    args = parser.parse_args()

    deadline = time.monotonic() + (args.wait or 0)
    with connect("worker") as conn:
        while True:
            text, finished = report(snapshot(conn))
            if finished or time.monotonic() >= deadline:
                break
            time.sleep(5)
    print(text)
    sys.exit(0 if finished else 1)


if __name__ == "__main__":
    main()
