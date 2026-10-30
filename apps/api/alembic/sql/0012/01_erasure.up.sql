-- 0012/01_erasure.up.sql
-- Google sign-in (ADR-0020): redact_user and purge_tenant keep 0011's bodies and also clear the
-- erased user's `google_sub` (so the Google account can never sign in to the pseudonymised row)
-- and delete the `login_flows` rows that still carry the user's email (a pending sign-in, sign-up
-- or link handle). Run as migrator, so users_credential_guard lets the google_sub write through.
-- purge_tenant data mode does both for exactly the users it pseudonymises, before their email
-- changes. Same signatures, SECURITY INVOKER, search_path and grants.
CREATE OR REPLACE FUNCTION public.redact_user(u uuid) RETURNS void
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  old_email text;
BEGIN
  SELECT x.email INTO old_email FROM public.users x WHERE x.id = u FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no user %', u USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.redact_user', u::text, true);
  DELETE FROM public.login_flows WHERE lower(email) = lower(old_email);
  UPDATE public.users SET email = 'erased+' || u || '@invalid', hashed_password = NULL,
                          display_name = NULL, erased_at = coalesce(erased_at, now()),
                          failed_auth_count = 0, locked_until = NULL,
                          password_must_change = false, google_sub = NULL
   WHERE id = u;
  DELETE FROM public.user_mfa_factors WHERE user_id = u;
  DELETE FROM public.user_recovery_codes WHERE user_id = u;
  DELETE FROM public.mfa_challenges WHERE user_id = u;
  DELETE FROM public.user_password_history WHERE user_id = u;
  UPDATE public.tenant_memberships SET is_active = false, deactivated_at = coalesce(deactivated_at, now())
   WHERE user_id = u AND is_active;
  DELETE FROM public.refresh_tokens WHERE user_id = u;
  DELETE FROM public.invitations
   WHERE accepted_user_id = u OR lower(email) = lower(old_email);
  UPDATE public.audit_log SET actor_email = NULL, ip = NULL
   WHERE actor_user_id = u AND (actor_email IS NOT NULL OR ip IS NOT NULL);
END
$$;

CREATE OR REPLACE FUNCTION public.purge_tenant(t uuid, mode text, force boolean DEFAULT false)
  RETURNS jsonb
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  tbl text;
  n bigint;
  counts jsonb := '{}'::jsonb;
  members uuid[];
  lifecycle record;
BEGIN
  IF mode NOT IN ('data', 'final') THEN
    RAISE EXCEPTION 'purge_tenant mode is data or final, not %', mode USING ERRCODE = 'GRD01';
  END IF;
  SELECT x.status, x.purge_after, x.data_purged_at INTO lifecycle FROM public.tenants x
   WHERE x.id = t FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no tenant %', t USING ERRCODE = 'GRD01';
  END IF;
  IF mode = 'data' AND NOT coalesce(force, false)
     AND NOT coalesce(lifecycle.status = 'offboarded' AND lifecycle.purge_after <= now(), false) THEN
    RAISE EXCEPTION 'purge_tenant data needs an offboarded tenant past purge_after (status %)',
      lifecycle.status USING ERRCODE = 'GRD01';
  END IF;
  IF mode = 'final' AND lifecycle.data_purged_at IS NULL THEN
    RAISE EXCEPTION 'purge_tenant final runs after the data phase' USING ERRCODE = 'GRD01';
  END IF;
  IF mode = 'final' AND NOT coalesce(force, false)
     AND lifecycle.data_purged_at > now() - interval '24 months' THEN
    RAISE EXCEPTION 'purge_tenant final runs after the 24-month audit retention'
      USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.purge_tenant', t::text, true);
  SET CONSTRAINTS ALL DEFERRED;

  IF mode = 'data' THEN
    FOREACH tbl IN ARRAY public.purge_tenant_order() LOOP
      EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1', tbl) USING t;
      GET DIAGNOSTICS n = ROW_COUNT;
      counts := counts || jsonb_build_object(tbl, n);
    END LOOP;
    UPDATE public.audit_log SET actor_email = NULL, ip = NULL
     WHERE tenant_id = t AND (actor_email IS NOT NULL OR ip IS NOT NULL);
    SELECT coalesce(array_agg(m.user_id), ARRAY[]::uuid[]) INTO members
      FROM public.tenant_memberships m
     WHERE m.tenant_id = t
       AND NOT EXISTS (SELECT 1 FROM public.tenant_memberships o
                        WHERE o.user_id = m.user_id AND o.tenant_id <> t);
    DELETE FROM public.user_mfa_factors WHERE user_id = ANY (members);
    DELETE FROM public.user_recovery_codes WHERE user_id = ANY (members);
    DELETE FROM public.mfa_challenges WHERE user_id = ANY (members);
    DELETE FROM public.user_password_history WHERE user_id = ANY (members);
    DELETE FROM public.login_flows f USING public.users x
     WHERE x.id = ANY (members) AND lower(f.email) = lower(x.email);
    UPDATE public.users u SET email = 'erased+' || u.id || '@invalid', hashed_password = NULL,
                              display_name = NULL, erased_at = coalesce(u.erased_at, now()),
                              failed_auth_count = 0, locked_until = NULL,
                              password_must_change = false, google_sub = NULL
     WHERE u.id = ANY (members);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('users_pseudonymised', n);
    UPDATE public.tenants SET data_purged_at = now() WHERE id = t;
  ELSE
    SELECT array_agg(m.user_id) INTO members FROM public.tenant_memberships m WHERE m.tenant_id = t;
    DELETE FROM public.audit_log WHERE tenant_id = t;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('audit_log', n);
    DELETE FROM public.tenants WHERE id = t;
    counts := counts || jsonb_build_object('tenant_memberships', coalesce(cardinality(members), 0));
    DELETE FROM public.users u
     WHERE u.id = ANY (coalesce(members, ARRAY[]::uuid[]))
       AND NOT EXISTS (SELECT 1 FROM public.tenant_memberships o WHERE o.user_id = u.id);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('users', n);
  END IF;

  INSERT INTO public.audit_log (tenant_id, actor_type, action, entity_type, entity_id, after)
  VALUES (NULL, 'system', 'tenant.purged', 'tenant', t,
          jsonb_build_object('mode', mode, 'row_counts', counts,
                             'backups_expire_by', now() + interval '35 days'));
  RETURN counts;
END
$$;
