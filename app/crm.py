"""Talking to a tenant's CRM, and choosing a slot.

The CRM (crm_mock/README.md) is slow, rate-limited and sometimes fails. This module handles one request at a
time; ordering, ownership and "what was already sent" live in app/steps/crm.py.

- Rate limit: 2 requests per second, counted in fixed one-second windows, and rejected requests count too.
  Rate-limited requests (`GET /slots`, every `POST`) are spaced SPACING_SECONDS apart, measured with the
  database clock and stored per CRM (`crm_pacing`), so the spacing holds when another worker takes over
  the CRM. `GET /bookings` and `GET /complaints` are not rate-limited by the CRM and are not paced.
- Failures: 429, 5xx, timeouts and dropped connections are retried here a few times (honouring
  `Retry-After`), then raised as TransientError for the queue's backoff. Only a few tries, because the
  caller holds the CRM's lane meanwhile and every other job for that CRM waits.
- A slow request takes 8 s and then answers normally, so the timeout is above that.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.pipeline import PermanentError, TransientError

SPACING_SECONDS = 0.6          # ~1.6 requests/second, under the CRM's 2
IN_STEP_TRIES = 3
TIMEOUT = httpx.Timeout(15, connect=5)


@dataclass(frozen=True)
class Response:
    status: int
    body: dict
    replayed: bool   # the CRM answered from its idempotency store: the write had already happened


def db_pacer(conn, crm_base_url: str, sleep: Callable[[float], None] = time.sleep) -> Callable[[], None]:
    """Wait until SPACING_SECONDS have passed since the last rate-limited request to this CRM, then claim now.

    Only the worker holding the CRM's lane sends rate-limited requests, so read-then-write is enough.
    """
    def pace() -> None:
        row = conn.execute(
            """
            SELECT GREATEST(0, EXTRACT(EPOCH FROM last_request_at + make_interval(secs => %s) - clock_timestamp()))
                   AS wait
            FROM crm_pacing WHERE crm_base_url = %s
            """,
            [SPACING_SECONDS, crm_base_url],
        ).fetchone()
        if row and row["wait"] > 0:
            sleep(float(row["wait"]))
        conn.execute(
            """
            INSERT INTO crm_pacing (crm_base_url, last_request_at) VALUES (%s, clock_timestamp())
            ON CONFLICT (crm_base_url) DO UPDATE SET last_request_at = EXCLUDED.last_request_at
            """,
            [crm_base_url],
        )
    return pace


class CrmClient:
    def __init__(self, base_url: str, pace: Callable[[], None] = lambda: None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.pace = pace
        self.http = http or httpx.Client(timeout=TIMEOUT)
        self.sleep = sleep

    def get(self, path: str, paced: bool) -> dict:
        r = self._send("GET", path, paced=paced)
        if r.status_code != 200:
            raise PermanentError(f"GET {path}: HTTP {r.status_code}")
        return r.json()

    def post(self, path: str, idempotency_key: str, body: str) -> Response:
        """`body` is sent exactly as given (the caller stored these bytes before sending them)."""
        r = self._send("POST", path, paced=True, key=idempotency_key, body=body)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return Response(r.status_code, data, r.headers.get("Idempotent-Replayed") == "true")

    def _send(self, method: str, path: str, *, paced: bool, key: str | None = None,
              body: str | None = None) -> httpx.Response:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if key:
            headers["Idempotency-Key"] = key
        error: TransientError | None = None
        for attempt in range(1, IN_STEP_TRIES + 1):
            if paced:
                self.pace()
            try:
                r = self.http.request(method, self.base_url + path, headers=headers,
                                      content=body.encode() if body is not None else None)
            except httpx.TransportError as exc:  # timeout, refused, or closed before the answer arrived
                error = TransientError(f"{method} {path}: {type(exc).__name__}")
            else:
                if r.status_code != 429 and r.status_code < 500:
                    return r
                error = TransientError(f"{method} {path}: HTTP {r.status_code}", _retry_after(r))
            if attempt < IN_STEP_TRIES:
                self.sleep(max(error.retry_after or 0, 0.5 * attempt))
        raise error


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


# --- Slot choice ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    slot_start: str | None        # the CRM's own string for the slot, sent back unchanged; None = follow-up
    reason: str                   # why this slot, or why none (the follow-up reason)
    moved: bool = False           # not the time the caller asked for: someone has to tell them


def choose_slot(slots: list[dict], kind: str, start: datetime | None, end: datetime | None,
                call_time: datetime) -> Choice:
    """The earliest available slot that matches the request. Only slots after the call count.

    A request with neither a day nor a time is not booked: there is nothing to match,
    so it goes to a person to follow up.
    """
    free = sorted((datetime.fromisoformat(s["start"]), s["start"]) for s in slots if s["available"])
    free = [(t, s) for t, s in free if t > call_time]
    if kind == "exact":
        fits = [s for t, s in free if t >= start]
        if not fits:
            return Choice(None, "no available slot at or after the requested time")
        same = datetime.fromisoformat(fits[0]) == start
        return Choice(fits[0], "requested time" if same else
                      "requested time not available; earliest available slot after it", moved=not same)
    if kind == "window":
        fits = [s for t, s in free if start <= t < end]
        if not fits:
            return Choice(None, "no available slot in the requested window")
        return Choice(fits[0], "earliest available slot in the requested window")
    if kind == "none":
        return Choice(None, "no day or time was agreed in the call; the caller must be asked")
    raise ValueError(f"unknown request kind {kind!r}")


FORMATS = {"online": "online", "in_person": "in person", "unknown": "not discussed"}


def booking_note(company: str | None, topic: str, meeting_format: str, moved: bool, also_complaint: bool) -> str:
    """What the team needs to prepare and follow up, without personal data: the caller's company (a
    company is not personal data), what they want to talk about, online or in person, whether the time differs from what the caller
    asked for, and whether the same call raised a complaint. Built only from stored values, so the same call
    always gives the same text (the body is stored and re-sent byte for byte)."""
    lines = [f"Company: {company or 'not given in the call'}",
             f"Topic: {topic or 'not extracted'}",
             f"Format: {FORMATS.get(meeting_format, 'not discussed')}"]
    if moved:
        lines.append("Time: the caller asked for another time (see requested_start), which was not available. "
                     "Confirm this one with them.")
    if also_complaint:
        lines.append("The same call also raised a complaint (see complaints, same call_id).")
    return "\n".join(lines)


def booking_body(call_id: str, choice: Choice, requested_start: datetime | None, timezone: str, note: str) -> str:
    body = {
        "call_id": call_id,
        "slot_start": choice.slot_start,
        "requested_start": requested_start.astimezone(ZoneInfo(timezone)).isoformat() if requested_start else None,
        "note": note,
    }
    return json.dumps(body, ensure_ascii=False)


def complaint_body(call_id: str, summary: str) -> str:
    return json.dumps({"call_id": call_id, "summary": summary}, ensure_ascii=False)


def booking_key(tenant_id: str, call_id: str, slot_start: str) -> str:
    return f"v1|booking|{tenant_id}|{call_id}|{slot_start}"


def complaint_key(tenant_id: str, call_id: str) -> str:
    return f"v1|complaint|{tenant_id}|{call_id}"
