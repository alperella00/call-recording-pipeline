# crm_mock

A stand-in for a tenant's CRM, used by `docker compose` (one instance per tenant) and by the CRM tests
(in-process). Standard library only.

```bash
python crm_mock/server.py        # port 8080; CHAOS=1 by default
```

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port |
| `CHAOS` | `1` | `1` = failure modes on, `0` = well-behaved |
| `CHAOS_SEED` | unset | Makes the failure sequence reproducible |
| `STATE_FILE` | `crm_mock/state.json` | Bookings and complaints survive restarts; delete it to reset |
| `CALENDAR_FILE` | `crm_mock/calendar.json` | Slots: first day, number of days, start hours, slots taken in advance |

## API

Every `POST` needs an `Idempotency-Key` header. The same key with the same body returns the stored answer with
`Idempotent-Replayed: true` and writes nothing; the same key with a different body is `422`.

| Request | Answers |
|---|---|
| `GET /slots` | `{"timezone", "slots": [{"start", "end", "available"}]}` |
| `POST /bookings` `{"call_id", "slot_start", "requested_start"?, "note"?}` | `201` booking, `409` slot taken, `422` unknown slot or missing fields |
| `POST /complaints` `{"call_id", "summary"}` | `201` complaint, `422` missing fields |
| `GET /bookings`, `GET /complaints` | everything stored (never affected by chaos) |
| `GET /health` | always `200` |

## Failure modes (`CHAOS=1`)

On `GET /slots` and every `POST`:
- `429` above 2 requests per second (fixed one-second windows; rejected requests count), with `Retry-After: 1`
- `500` on 15% of requests
- an 8-second stall on 5%
- on 5% of successful writes, the write is committed and the connection is closed before the answer is sent

429 and 500 answers are not stored under the idempotency key, so retrying with the same key is processed normally.
