-- What the CRM step sends, written down *before* it is sent.
--
-- crm_actions: one row per idempotency key. A row is inserted with the exact request body before the POST
--              and stays `pending` until the CRM's answer is known. A worker that takes over a job (the
--              previous one died, or the request timed out) finds the pending row and re-sends the same
--              bytes with the same key, instead of choosing a new slot.
--              request_body is text, not jsonb: jsonb reorders keys and normalises whitespace, and the CRM
--              compares bodies, so what is re-sent must be byte-for-byte what was sent.
-- crm_pacing:  when the last rate-limited request went to each CRM, so the 2 requests/second limit holds
--              when the CRM "lane" passes from one worker to another. Holds no tenant data.

CREATE TABLE crm_actions (
    tenant_id       text NOT NULL,
    recording_id    text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('booking', 'complaint')),
    idempotency_key text NOT NULL,
    request_body    text NOT NULL,
    -- pending: outcome unknown (not sent yet, or sent without a definite answer)
    -- succeeded: 200/201; rejected: a definite "no" (409 slot taken, 422)
    status          text NOT NULL CHECK (status IN ('pending', 'succeeded', 'rejected')),
    http_status     integer,
    crm_id          text,
    attempts        integer NOT NULL DEFAULT 0,   -- times a step sent it (each up to IN_STEP_TRIES HTTP tries)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

-- While an action's outcome is unknown, no other action of the same kind may be started for that call.
CREATE UNIQUE INDEX crm_actions_one_open ON crm_actions (tenant_id, recording_id, kind) WHERE status = 'pending';

ALTER TABLE crm_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_actions FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON crm_actions
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE TABLE crm_pacing (
    crm_base_url    text PRIMARY KEY,
    last_request_at timestamptz NOT NULL
);

GRANT SELECT ON crm_actions TO app_api;
GRANT SELECT, INSERT, UPDATE ON crm_actions, crm_pacing TO app_worker;
