-- 0017/02_tenant_recovery.up.sql
-- SI5 (ADR-0029 §5): trial recovery. Two migrator-owned SECURITY DEFINER functions, with a pinned
-- search_path and EXECUTE for app_rw only, beside 0004's set_tenant_lifecycle (unchanged):
--   activate_tenant(p_tenant, p_reason, p_staff_email): trial or suspended → active;
--   extend_tenant_trial(p_tenant, p_days, p_reason, p_staff_email): a trial ends
--     max(trial_ends_at, now()) + days; a tenant whose newest `tenant.suspended` audit reason is
--     `trial_expired` returns to trial ending now() + days.
-- The caller is staff (session_user migrator, or a superuser login that switched to migrator: the
-- CLI tests) with a staff email, or a platform admin (`app_is_platform_admin()`), whose own email
-- is the audit actor. Neither is a member of the brand's tenant, so the audit row is
-- `actor_type 'staff'` (fk_audit_log_actor_user_id names a membership). Authorization, a reason
-- outside 1..500 characters and days outside 1..90 raise GRD01; the outcome is returned as text:
-- 'not_found' | 'unchanged' | 'refused' | 'activated' | 'extended'.
CREATE FUNCTION public.activate_tenant(p_tenant uuid, p_reason text, p_staff_email text)
  RETURNS text
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  staff boolean := session_user = 'migrator'
    OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = session_user), false);
  actor text;
  current_status text;
BEGIN
  IF staff THEN
    actor := p_staff_email;
  ELSIF public.app_is_platform_admin() THEN
    SELECT u.email INTO actor FROM public.users u WHERE u.id = public.app_user_id();
  ELSE
    RAISE EXCEPTION 'only staff or a platform admin may activate a tenant' USING ERRCODE = 'GRD01';
  END IF;
  IF actor IS NULL OR btrim(actor) = '' THEN
    RAISE EXCEPTION 'a tenant activation names its staff email' USING ERRCODE = 'GRD01';
  END IF;
  IF p_reason IS NULL OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 500 THEN
    RAISE EXCEPTION 'a tenant activation needs a reason of 1 to 500 characters'
      USING ERRCODE = 'GRD01';
  END IF;
  SELECT x.status INTO current_status FROM public.tenants x WHERE x.id = p_tenant FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'not_found';
  END IF;
  IF current_status = 'active' THEN
    RETURN 'unchanged';
  END IF;
  IF current_status NOT IN ('trial', 'suspended') THEN
    RETURN 'refused';
  END IF;
  UPDATE public.tenants SET status = 'active' WHERE id = p_tenant;
  PERFORM public.audit(p_tenant, 'staff', NULL, 'tenant.active', 'tenant', p_tenant, NULL, NULL,
                       p_reason, NULL, NULL, NULL, actor);
  RETURN 'activated';
END
$$;

CREATE FUNCTION public.extend_tenant_trial(p_tenant uuid, p_days integer, p_reason text,
                                           p_staff_email text)
  RETURNS text
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  staff boolean := session_user = 'migrator'
    OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = session_user), false);
  actor text;
  t record;
  last_suspension text;
BEGIN
  IF staff THEN
    actor := p_staff_email;
  ELSIF public.app_is_platform_admin() THEN
    SELECT u.email INTO actor FROM public.users u WHERE u.id = public.app_user_id();
  ELSE
    RAISE EXCEPTION 'only staff or a platform admin may extend a trial' USING ERRCODE = 'GRD01';
  END IF;
  IF actor IS NULL OR btrim(actor) = '' THEN
    RAISE EXCEPTION 'a trial extension names its staff email' USING ERRCODE = 'GRD01';
  END IF;
  IF p_reason IS NULL OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 500 THEN
    RAISE EXCEPTION 'a trial extension needs a reason of 1 to 500 characters'
      USING ERRCODE = 'GRD01';
  END IF;
  IF p_days IS NULL OR p_days NOT BETWEEN 1 AND 90 THEN
    RAISE EXCEPTION 'a trial extension is 1 to 90 days' USING ERRCODE = 'GRD01';
  END IF;
  SELECT x.status, x.trial_ends_at INTO t FROM public.tenants x WHERE x.id = p_tenant FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'not_found';
  END IF;
  IF t.status = 'trial' THEN
    UPDATE public.tenants
       SET trial_ends_at = greatest(coalesce(t.trial_ends_at, now()), now())
                           + make_interval(days => p_days)
     WHERE id = p_tenant;
  ELSIF t.status = 'suspended' THEN
    SELECT a.reason INTO last_suspension FROM public.audit_log a
     WHERE a.tenant_id = p_tenant AND a.entity_type = 'tenant' AND a.entity_id = p_tenant
       AND a.action = 'tenant.suspended'
     ORDER BY a.occurred_at DESC LIMIT 1;
    IF last_suspension IS DISTINCT FROM 'trial_expired' THEN
      RETURN 'refused';
    END IF;
    UPDATE public.tenants
       SET status = 'trial', trial_ends_at = now() + make_interval(days => p_days)
     WHERE id = p_tenant;
  ELSE
    RETURN 'refused';
  END IF;
  PERFORM public.audit(p_tenant, 'staff', NULL, 'tenant.trial_extended', 'tenant', p_tenant, NULL,
                       NULL, p_reason, NULL, NULL, NULL, actor);
  RETURN 'extended';
END
$$;

REVOKE ALL ON FUNCTION public.activate_tenant(uuid, text, text),
  public.extend_tenant_trial(uuid, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.activate_tenant(uuid, text, text),
  public.extend_tenant_trial(uuid, integer, text, text) TO app_rw;

-- A platform admin reads every tenant (the admin console list), as users_platform_admin (0010).
CREATE POLICY tenants_platform_admin ON public.tenants FOR SELECT TO app_rw
  USING ((SELECT public.app_is_platform_admin()));
