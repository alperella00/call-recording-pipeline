-- API keys: which tenant a caller of the API acts as.
--
-- Only the sha256 of a key is stored; the key itself is shown once when it is created
-- (python -m app.api_keys create <tenant>). Keys are 256 random bits, so a fast hash is enough: there is
-- nothing to guess, unlike a password.
--
-- The api role cannot read this table at all. It can only call tenant_for_api_key(), which answers "which
-- tenant is this key for?" for one key at a time. A bug in the api can't list keys or hashes, and has no
-- way to pick a tenant other than the one the caller's key belongs to.

CREATE TABLE api_keys (
    key_hash   bytea PRIMARY KEY,                   -- sha256 of the key
    tenant_id  text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    label      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

-- Same rule as every tenant table (and the safety-net test checks it). Nobody but the owner uses it anyway.
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON api_keys
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- SECURITY DEFINER: runs with the privileges of its owner (the migration's superuser), so it can read
-- api_keys although its caller can't. The fixed search_path stops a caller from planting objects that the
-- function would pick up instead.
CREATE FUNCTION tenant_for_api_key(api_key text) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT tenant_id FROM public.api_keys
    WHERE key_hash = sha256(convert_to(api_key, 'UTF8')) AND revoked_at IS NULL
$$;

REVOKE ALL ON FUNCTION tenant_for_api_key(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant_for_api_key(text) TO app_api;
