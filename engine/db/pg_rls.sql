-- Row-level security policies.
--
-- Applied separately from the schema so the policy set can be reviewed on
-- its own. This file is the entire tenant-isolation guarantee; read it as
-- a security document, not as plumbing.
--
-- The app connects as a role with NOSUPERUSER and NOBYPASSRLS, sets
--   SET LOCAL app.org_id = '<uuid>'
-- inside a transaction, and every statement is then filtered to that org.
-- SET LOCAL is deliberate: it is scoped to the transaction, so a pooled
-- connection cannot carry one tenant's context into another's request.

-- current_setting(..., true) returns NULL rather than erroring when the
-- setting is absent. A NULL org context matches no rows, which is the
-- correct failure mode: unset context sees nothing, never everything.
CREATE OR REPLACE FUNCTION app_current_org() RETURNS UUID AS $$
  SELECT NULLIF(current_setting('app.org_id', true), '')::UUID;
$$ LANGUAGE SQL STABLE;

-- Two tiers of enforcement, and the difference is deliberate.
--
-- DATA tables get ENABLE + FORCE. FORCE means even the table owner obeys
-- the policy, so a migration script or an accidental owner connection
-- cannot read across organisations either.
--
-- IDENTITY tables (org, app_user, session) get ENABLE only. They must stay
-- reachable by the SECURITY DEFINER functions below, which run as the
-- schema owner and handle signup and login, both of which necessarily
-- happen before any org context exists. The application never connects as
-- the owner, so ENABLE alone still fully constrains it; what is given up
-- is protection against the owner role, which is used only by migrations.
DO $$
DECLARE
  t TEXT;
  data_tables TEXT[] := ARRAY[
    'entity','location','item','snapshot','fact_inventory','fact_demand',
    'metric_run','metric_result','threshold_set','audit'
  ];
  identity_tables TEXT[] := ARRAY['org','app_user','session'];
BEGIN
  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS org_isolation ON %I', t);
    EXECUTE format($f$
      CREATE POLICY org_isolation ON %I
        USING (org_id = app_current_org())
        WITH CHECK (org_id = app_current_org())
    $f$, t);
  END LOOP;

  FOREACH t IN ARRAY identity_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS org_isolation ON %I', t);
  END LOOP;
END $$;

-- org is matched on its own primary key; the others on org_id.
CREATE POLICY org_isolation ON org
  USING (org_id = app_current_org())
  WITH CHECK (org_id = app_current_org());
CREATE POLICY org_isolation ON app_user
  USING (org_id = app_current_org())
  WITH CHECK (org_id = app_current_org());
CREATE POLICY org_isolation ON session
  USING (org_id = app_current_org())
  WITH CHECK (org_id = app_current_org());

-- Provisioning is a chicken-and-egg case: creating an organisation happens
-- before any org context can exist, so the policy on org would block its
-- own first insert. These two functions are the only privileged path, and
-- they are deliberately narrow: they create exactly one row each and
-- return nothing that could be used to read existing data.
--
-- provision_org is the signup path and must be rate limited at the edge.
-- provision_user creates a user inside an org; the application layer
-- restricts it to an owner of that same org, which RLS cannot express
-- because the caller is acting on a row that does not exist yet.
CREATE OR REPLACE FUNCTION provision_org(
  p_org_id UUID, p_name TEXT, p_country TEXT
) RETURNS VOID
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  INSERT INTO org (org_id, name, country) VALUES (p_org_id, p_name, p_country);
$$;

CREATE OR REPLACE FUNCTION provision_user(
  p_user_id UUID, p_org_id UUID, p_email TEXT, p_display_name TEXT,
  p_password_hash TEXT, p_role TEXT
) RETURNS VOID
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  INSERT INTO app_user (user_id, org_id, email, display_name, password_hash, role)
  VALUES (p_user_id, p_org_id, lower(p_email), p_display_name,
          p_password_hash, p_role);
$$;

-- Login happens before any org context exists, so the lookup of a user by
-- email needs a path that RLS would otherwise block. It is a SECURITY
-- DEFINER function returning only what authentication needs, never the
-- whole row set: no way to enumerate users, no way to read another org's
-- data through it.
CREATE OR REPLACE FUNCTION auth_lookup(p_email TEXT)
RETURNS TABLE (
  user_id UUID, org_id UUID, password_hash TEXT, role TEXT,
  display_name TEXT, is_active BOOLEAN, failed_logins INTEGER,
  locked_until TIMESTAMPTZ
)
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  SELECT u.user_id, u.org_id, u.password_hash, u.role, u.display_name,
         u.is_active, u.failed_logins, u.locked_until
  FROM app_user u
  WHERE lower(u.email) = lower(p_email)
  LIMIT 1;
$$;

-- Resolving a session token likewise precedes org context.
CREATE OR REPLACE FUNCTION auth_session(p_token_hash TEXT)
RETURNS TABLE (
  session_id UUID, user_id UUID, org_id UUID, role TEXT,
  display_name TEXT, email TEXT, expires_at TIMESTAMPTZ
)
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  SELECT s.session_id, s.user_id, s.org_id, u.role, u.display_name, u.email,
         s.expires_at
  FROM session s JOIN app_user u ON u.user_id = s.user_id
  WHERE s.token_hash = p_token_hash
    AND s.revoked_at IS NULL
    AND s.expires_at > now()
    AND u.is_active
  LIMIT 1;
$$;

-- Writes during authentication: creating a session, recording a failed
-- attempt, clearing the counter on success. All narrow, all by primary key.
CREATE OR REPLACE FUNCTION auth_create_session(
  p_session_id UUID, p_token_hash TEXT, p_user_id UUID, p_org_id UUID,
  p_expires_at TIMESTAMPTZ, p_user_agent TEXT, p_ip TEXT
) RETURNS VOID
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  INSERT INTO session (session_id, token_hash, user_id, org_id, expires_at,
                       user_agent, ip)
  VALUES (p_session_id, p_token_hash, p_user_id, p_org_id, p_expires_at,
          p_user_agent, p_ip);
  UPDATE app_user SET last_login_at = now(), failed_logins = 0,
                      locked_until = NULL
  WHERE user_id = p_user_id;
$$;

CREATE OR REPLACE FUNCTION auth_record_failure(p_email TEXT)
RETURNS VOID
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  UPDATE app_user
  SET failed_logins = failed_logins + 1,
      locked_until = CASE WHEN failed_logins + 1 >= 8
                          THEN now() + interval '15 minutes' END
  WHERE lower(email) = lower(p_email);
$$;

CREATE OR REPLACE FUNCTION auth_revoke(p_token_hash TEXT) RETURNS VOID
LANGUAGE SQL SECURITY DEFINER SET search_path = public AS $$
  UPDATE session SET revoked_at = now()
  WHERE token_hash = p_token_hash AND revoked_at IS NULL;
$$;
