-- 0004/19_redact_prune.up.sql
-- DATABASE.md §9 "Pruning" (:1847-1849), the retention table (:1830-1840) and "User erasure"
-- (:1876); §8.1 migrator-only functions (SECURITY INVOKER, EXECUTE granted to nobody). Each sets
-- the GUC the append-only, issue and task guards check; refusals → GRD01.
-- redact_user(u): the email becomes a placeholder, the password hash and display name are
--   cleared, erased_at is set; memberships are deactivated; refresh tokens and the user's
--   invitations are deleted; actor_email and ip are nulled in the user's audit rows.
-- prune_audit_log(cutoff): audit rows older than cutoff; the floor is the 24-month retention.
-- prune_append_only(table, cutoff): agent_runs or validation_runs older than cutoff (13 months);
--   review_decisions live as long as the tenant and are never pruned.
-- prune_closed(table, cutoff): closed listing_issues / terminal human_tasks older than cutoff
--   (13 months); open rows live as long as their listing.
CREATE FUNCTION public.redact_user(u uuid) RETURNS void
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
  UPDATE public.users SET email = 'erased+' || u || '@invalid', hashed_password = NULL,
                          display_name = NULL, erased_at = coalesce(erased_at, now())
   WHERE id = u;
  UPDATE public.tenant_memberships SET is_active = false, deactivated_at = coalesce(deactivated_at, now())
   WHERE user_id = u AND is_active;
  DELETE FROM public.refresh_tokens WHERE user_id = u;
  DELETE FROM public.invitations
   WHERE accepted_user_id = u OR lower(email) = lower(old_email);
  UPDATE public.audit_log SET actor_email = NULL, ip = NULL
   WHERE actor_user_id = u AND (actor_email IS NOT NULL OR ip IS NOT NULL);
END
$$;

CREATE FUNCTION public.prune_audit_log(cutoff timestamptz) RETURNS bigint
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  n bigint;
BEGIN
  IF cutoff > now() - interval '24 months' THEN
    RAISE EXCEPTION 'cutoff % is inside the 24-month audit retention', cutoff
      USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.prune_table', 'public.audit_log', true);
  DELETE FROM public.audit_log WHERE occurred_at < cutoff;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;

CREATE FUNCTION public.prune_append_only(t regclass, cutoff timestamptz) RETURNS bigint
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  n bigint;
BEGIN
  IF t NOT IN ('public.agent_runs'::regclass, 'public.validation_runs'::regclass) THEN
    RAISE EXCEPTION 'prune_append_only prunes agent_runs or validation_runs, not %', t
      USING ERRCODE = 'GRD01';
  END IF;
  IF cutoff > now() - interval '13 months' THEN
    RAISE EXCEPTION 'cutoff % is inside the 13-month retention of %', cutoff, t
      USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.prune_table', t::text, true);
  EXECUTE format('DELETE FROM %s WHERE created_at < $1', t) USING cutoff;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;

CREATE FUNCTION public.prune_closed(t regclass, cutoff timestamptz) RETURNS bigint
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  n bigint;
BEGIN
  IF t NOT IN ('public.listing_issues'::regclass, 'public.human_tasks'::regclass) THEN
    RAISE EXCEPTION 'prune_closed prunes listing_issues or human_tasks, not %', t
      USING ERRCODE = 'GRD01';
  END IF;
  IF cutoff > now() - interval '13 months' THEN
    RAISE EXCEPTION 'cutoff % is inside the 13-month retention of %', cutoff, t
      USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.prune_table', t::text, true);
  IF t = 'public.listing_issues'::regclass THEN
    DELETE FROM public.listing_issues WHERE status <> 'open' AND resolved_at < cutoff;
  ELSE
    DELETE FROM public.human_tasks
     WHERE status IN ('applied', 'cancelled') AND updated_at < cutoff;
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;
