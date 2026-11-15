-- Tenants, recordings and the processing state of each recording.
--
-- Every table holding tenant data has row-level security (RLS) enabled and FORCED, with a policy that
-- only shows rows of the tenant set by `app.tenant_id` for the current transaction (see app/db.py).
-- If no tenant is set, current_setting(..., true) is NULL and the policy matches nothing.

CREATE TABLE tenants (
    id               text PRIMARY KEY CHECK (id ~ '^[a-z0-9_-]+$'),   -- slug, stable across DB resets
    name             text NOT NULL,
    timezone         text NOT NULL DEFAULT 'Europe/Istanbul',
    assistant_name   text,            -- the voice assistant's persona name; not personal data, not masked
    crm_base_url     text NOT NULL,   -- each tenant has its own CRM
    min_notice_hours integer NOT NULL DEFAULT 4 CHECK (min_notice_hours >= 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE recordings (
    tenant_id    text NOT NULL REFERENCES tenants (id),
    id           text NOT NULL,       -- id from the manifest, e.g. rec_001; unique per tenant only
    audio_path   text NOT NULL,
    audio_sha256 text,                -- identifies byte-identical uploads of the same call
    recorded_at  timestamptz NOT NULL,
    language     text NOT NULL DEFAULT 'tr',
    duplicate_of text,                -- set when the same audio was already uploaded as another recording
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    -- composite key: a recording can only be a duplicate of a recording of the same tenant
    FOREIGN KEY (tenant_id, duplicate_of) REFERENCES recordings (tenant_id, id)
);

-- One row per recording: where it is in the pipeline and why it stopped, if it did.
-- Holds no personal data (last_error must never contain transcript text).
CREATE TABLE jobs (
    tenant_id    text NOT NULL,
    recording_id text NOT NULL,
    step         text NOT NULL DEFAULT 'transcribe'
                 CHECK (step IN ('transcribe', 'mask', 'extract', 'crm', 'done')),
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'retry_wait', 'failed', 'done')),
    attempts     integer NOT NULL DEFAULT 0,
    run_at       timestamptz NOT NULL DEFAULT now(),   -- not picked up before this time (retry backoff)
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX jobs_ready ON jobs (run_at) WHERE status IN ('queued', 'retry_wait');

-- Row-level security -------------------------------------------------------------------------------

ALTER TABLE tenants    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants    FORCE  ROW LEVEL SECURITY;
ALTER TABLE recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings FORCE  ROW LEVEL SECURITY;
ALTER TABLE jobs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs       FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenants
    USING (id = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation ON recordings
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation ON jobs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- The worker has to find work across all tenants, so it may see every row of the queue table.
-- It still reads and writes tenant data (recordings, transcripts, ...) only as one tenant.
CREATE POLICY worker_sees_queue ON jobs TO app_worker
    USING (true)
    WITH CHECK (true);

-- Privileges ---------------------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO app_api, app_worker;
GRANT SELECT ON tenants, recordings, jobs TO app_api;
GRANT SELECT ON tenants TO app_worker;
GRANT SELECT, INSERT, UPDATE ON recordings, jobs TO app_worker;
