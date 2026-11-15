-- What each call asked for, and what was done about it.
--
-- extractions: the extractor's reading of the masked transcript, stored once and reused on every retry, so the
--              requests sent to the CRM never change between attempts.
-- meetings:    what the caller asked for (requested_*) and what was booked (booked_*), or why it needs a human.
-- complaints:  the short summary sent to the CRM.
-- At most one meeting request and one complaint per call.

CREATE TABLE extractions (
    tenant_id      text NOT NULL,
    recording_id   text NOT NULL,
    provider       text NOT NULL,     -- anthropic | rules | none (no speech)
    model          text NOT NULL,
    prompt_version text NOT NULL,
    result         jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE meetings (
    tenant_id          text NOT NULL,
    recording_id       text NOT NULL,
    status             text NOT NULL CHECK (status IN ('pending', 'booked', 'needs_follow_up')),
    requested_kind     text NOT NULL CHECK (requested_kind IN ('exact', 'window', 'none')),
    requested_start    timestamptz,
    requested_end      timestamptz,
    requested_quote    text NOT NULL DEFAULT '',   -- the caller's words, masked
    booked_slot_start  timestamptz,
    crm_booking_id     text,
    follow_up_reason   text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE complaints (
    tenant_id          text NOT NULL,
    recording_id       text NOT NULL,
    status             text NOT NULL CHECK (status IN ('pending', 'sent')),
    summary            text NOT NULL,
    crm_complaint_id   text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE extractions ENABLE ROW LEVEL SECURITY;
ALTER TABLE extractions FORCE  ROW LEVEL SECURITY;
ALTER TABLE meetings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE meetings    FORCE  ROW LEVEL SECURITY;
ALTER TABLE complaints  ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaints  FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON extractions
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON meetings
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON complaints
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON meetings, complaints TO app_api;
GRANT SELECT, INSERT ON extractions TO app_worker;
GRANT SELECT, INSERT, UPDATE ON meetings, complaints TO app_worker;
