-- When the job started waiting (Deferred) at its current step, in this run. Waits are bounded from here, not from
-- when the request was created: after a reprocess or an outage, old requests must still be served in order.
-- Cleared when the job moves to another step, ends, or is re-queued by reprocess.

ALTER TABLE jobs ADD COLUMN waiting_since timestamptz;
