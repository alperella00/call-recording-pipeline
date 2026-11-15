"""A stand-in CRM for local runs and tests: meeting slots, bookings and complaints.

It behaves like a third-party API the pipeline does not control. With CHAOS=1 it rate-limits (2 requests per
second, fixed one-second windows, rejected requests count too), answers some requests with 500, stalls some
for 8 seconds, and sometimes commits a write and then drops the connection before answering.
GET /bookings, GET /complaints and GET /health are never affected.

Every POST needs an Idempotency-Key header. The same key with the same body returns the stored answer with
`Idempotent-Replayed: true` and writes nothing; the same key with another body is a 422. 429 and 500 answers
are not stored, so a retry with the same key is processed normally.

Standard library only. Run: python crm_mock/server.py   (PORT, CHAOS, CHAOS_SEED, STATE_FILE, CALENDAR_FILE)
"""

import hashlib
import json
import os
import random
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RATE_LIMIT_PER_SECOND = 2
ERROR_RATE = 0.15
SLOW_RATE = 0.05
SLOW_SECONDS = 8
LOST_RESPONSE_RATE = 0.05


def build_slots(calendar: dict) -> list[dict]:
    """One-hour slots on weekdays, from the calendar file: first day, number of days, start hours, taken slots."""
    tz = ZoneInfo(calendar["timezone"])
    taken = set(calendar.get("taken", []))
    first = date.fromisoformat(calendar["first_day"])
    slots = []
    for offset in range(calendar["days"]):
        day = first + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour in calendar["hours"]:
            start = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            slots.append({"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
                          "available": start.isoformat() not in taken})
    return slots


class Crm:
    """The CRM's data and rules. Thread-safe; persisted to STATE_FILE after every write when one is given."""

    def __init__(self, calendar: dict, state_file: Path | None = None, chaos: bool = False,
                 seed: str | None = None):
        self.lock = threading.Lock()
        self.chaos = chaos
        self.rng = random.Random(seed)
        self.state_file = state_file
        self.window_second, self.window_count = 0, 0
        if state_file and state_file.exists():
            self.state = json.loads(state_file.read_text())
        else:
            self.state = {"timezone": calendar["timezone"], "slots": build_slots(calendar),
                          "bookings": [], "complaints": [], "idempotency": {}}

    def _save(self) -> None:
        if self.state_file:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
            os.replace(tmp, self.state_file)

    # --- chaos -----------------------------------------------------------------------------------------

    def rate_limited(self) -> bool:
        with self.lock:
            now = int(time.time())
            if now != self.window_second:
                self.window_second, self.window_count = now, 0
            self.window_count += 1
            return self.window_count > RATE_LIMIT_PER_SECOND

    def roll(self, rate: float) -> bool:
        with self.lock:
            return self.chaos and self.rng.random() < rate

    # --- reads -----------------------------------------------------------------------------------------

    def read(self, what: str) -> dict:
        with self.lock:
            if what == "slots":
                return {"timezone": self.state["timezone"], "slots": [dict(s) for s in self.state["slots"]]}
            return {what: [dict(x) for x in self.state[what]]}

    # --- writes ----------------------------------------------------------------------------------------

    def post(self, path: str, key: str, raw: bytes) -> tuple[int, dict, bool]:
        """Returns (status, body, replayed)."""
        fingerprint = hashlib.sha256(path.encode() + b"\n" + raw).hexdigest()
        with self.lock:
            stored = self.state["idempotency"].get(key)
            if stored:
                if stored["fingerprint"] != fingerprint:
                    return 422, {"error": "Idempotency-Key reused with a different request"}, False
                return stored["status"], stored["body"], True
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return 400, {"error": "body is not JSON"}, False
            status, answer = self._bookings(body) if path == "/bookings" else self._complaints(body)
            self.state["idempotency"][key] = {"fingerprint": fingerprint, "status": status, "body": answer}
            self._save()
            return status, answer, False

    def _bookings(self, body: dict) -> tuple[int, dict]:
        if not body.get("call_id") or not body.get("slot_start"):
            return 422, {"error": "call_id and slot_start are required"}
        slot = next((s for s in self.state["slots"] if s["start"] == body["slot_start"]), None)
        if slot is None:
            return 422, {"error": "unknown slot"}
        if not slot["available"]:
            return 409, {"error": "slot already taken"}
        slot["available"] = False
        booking = {"id": "bk_" + uuid.uuid4().hex[:10], "call_id": body["call_id"],
                   "slot_start": slot["start"], "requested_start": body.get("requested_start"),
                   "note": body.get("note")}
        self.state["bookings"].append(booking)
        return 201, booking

    def _complaints(self, body: dict) -> tuple[int, dict]:
        if not body.get("call_id") or not body.get("summary"):
            return 422, {"error": "call_id and summary are required"}
        complaint = {"id": "cp_" + uuid.uuid4().hex[:10], "call_id": body["call_id"], "summary": body["summary"]}
        self.state["complaints"].append(complaint)
        return 201, complaint


class Handler(BaseHTTPRequestHandler):
    server_version = "crm-mock/1"
    quiet = False

    @property
    def crm(self) -> Crm:
        return self.server.crm

    def log_message(self, *args):
        if not self.quiet:
            super().log_message(*args)

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _chaos(self) -> bool:
        """Apply the failure modes to a rate-limited request. True = already answered."""
        if not self.crm.chaos:
            return False
        if self.crm.rate_limited():
            self._send(429, {"error": "too many requests"}, {"Retry-After": "1"})
            return True
        if self.crm.roll(ERROR_RATE):
            self._send(500, {"error": "internal error"})
            return True
        if self.crm.roll(SLOW_RATE):
            time.sleep(SLOW_SECONDS)
        return False

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path in ("/bookings", "/complaints"):
            return self._send(200, self.crm.read(self.path[1:]))
        if self.path == "/slots":
            if not self._chaos():
                self._send(200, self.crm.read("slots"))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/bookings", "/complaints"):
            return self._send(404, {"error": "not found"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        key = self.headers.get("Idempotency-Key")
        if not key:
            return self._send(400, {"error": "Idempotency-Key header is required"})
        if self._chaos():
            return
        status, body, replayed = self.crm.post(self.path, key, raw)
        if not replayed and status == 201 and self.crm.roll(LOST_RESPONSE_RATE):
            self.close_connection = True   # the write is committed; the caller never hears about it
            return
        self._send(status, body, {"Idempotent-Replayed": "true"} if replayed else None)


def make_server(crm: Crm, host: str = "127.0.0.1", port: int = 0, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("QuietHandler" if quiet else "Handler", (Handler,), {"quiet": quiet})
    server = ThreadingHTTPServer((host, port), handler)
    server.crm = crm
    return server


def main() -> None:
    calendar = json.loads(Path(os.environ.get("CALENDAR_FILE", HERE / "calendar.json")).read_text())
    crm = Crm(calendar, Path(os.environ.get("STATE_FILE", HERE / "state.json")),
              chaos=os.environ.get("CHAOS", "1") == "1", seed=os.environ.get("CHAOS_SEED"))
    port = int(os.environ.get("PORT", "8080"))
    print(f"crm-mock on :{port}, CHAOS={'1' if crm.chaos else '0'}", flush=True)
    make_server(crm, "0.0.0.0", port).serve_forever()


if __name__ == "__main__":
    main()
