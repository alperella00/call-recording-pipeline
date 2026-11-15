-- Transcripts.
--
-- raw_transcripts holds the speech-to-text output, which contains personal data in clear text. It exists only
-- between the transcribe and mask steps: the mask step writes the masked transcript and deletes the raw row in
-- the same transaction. No API endpoint reads this table.
--
-- transcripts holds the masked transcript, kept permanently. This is what the tenant can read.

CREATE TABLE raw_transcripts (
    tenant_id    text NOT NULL,
    recording_id text NOT NULL,
    provider     text NOT NULL,
    model        text NOT NULL,
    content      jsonb NOT NULL,      -- {"no_speech": bool, "channels": [{"index", "text", "words": [...]}]}
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE transcripts (
    tenant_id    text NOT NULL,
    recording_id text NOT NULL,
    provider     text NOT NULL,
    model        text NOT NULL,
    no_speech    boolean NOT NULL DEFAULT false,
    turns        jsonb NOT NULL,      -- masked dialogue: [{"speaker": "agent"|"caller", "start": s, "text": ...}]
    text         text NOT NULL,       -- the same dialogue as plain text, masked
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE raw_transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_transcripts FORCE  ROW LEVEL SECURITY;
ALTER TABLE transcripts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts     FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON raw_transcripts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation ON transcripts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- The api never sees raw transcripts, not even its own tenant's.
GRANT SELECT ON transcripts TO app_api;
GRANT SELECT, INSERT, DELETE ON raw_transcripts TO app_worker;
GRANT SELECT, INSERT ON transcripts TO app_worker;
