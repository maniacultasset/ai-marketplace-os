-- 0004/16_tenant_lifecycle.up.sql
-- DATABASE.md §5.1 tenants "Privileges" (:229) and §9 "Tenant lifecycle" (:1855-1870). The
-- lifecycle columns move only through these two migrator-owned SECURITY DEFINER functions, with a
-- pinned search_path and EXECUTE for app_rw; both write an audit row.
-- set_tenant_lifecycle(p_tenant, p_status, p_reason):
--   * app_rw acts only on its own tenant (p_tenant = app_tenant_id()) and may only offboard a
--     trial or active tenant; suspend, reactivate and every other move need the migrator login
--     (staff through app.cli);
--   * the edges are the §9 diagram: trial → active | suspended | offboarded; active →
--     suspended | offboarded; suspended → active | offboarded; offboarded → active before
--     purge_after and before any data purge;
--   * offboarding stamps offboarded_at and purge_after = + 30 days; a suspension revokes the
--     tenant's live refresh tokens (`tenant_suspended`).
-- expire_trials(): every `trial` whose trial_ends_at has passed → `suspended`, tokens revoked;
-- returns how many. Refusals → GRD01.
CREATE FUNCTION public.set_tenant_lifecycle(p_tenant uuid, p_status text, p_reason text)
  RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  t record;
  staff boolean := session_user = 'migrator';
BEGIN
  IF NOT staff AND p_tenant IS DISTINCT FROM public.app_tenant_id() THEN
    RAISE EXCEPTION 'set_tenant_lifecycle acts only on the caller''s tenant' USING ERRCODE = 'GRD01';
  END IF;
  SELECT x.status, x.purge_after, x.data_purged_at INTO t FROM public.tenants x
   WHERE x.id = p_tenant FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no tenant %', p_tenant USING ERRCODE = 'GRD01';
  END IF;
  IF (t.status, p_status) NOT IN (
       ('trial', 'active'), ('trial', 'suspended'), ('trial', 'offboarded'),
       ('active', 'suspended'), ('active', 'offboarded'),
       ('suspended', 'active'), ('suspended', 'offboarded'), ('offboarded', 'active')) THEN
    RAISE EXCEPTION 'tenant lifecycle % -> % refused', t.status, p_status USING ERRCODE = 'GRD01';
  END IF;
  IF NOT staff AND NOT (p_status = 'offboarded' AND t.status IN ('trial', 'active')) THEN
    RAISE EXCEPTION 'only staff may move a tenant % -> %', t.status, p_status USING ERRCODE = 'GRD01';
  END IF;
  IF t.status = 'offboarded' AND (t.data_purged_at IS NOT NULL OR t.purge_after <= now()) THEN
    RAISE EXCEPTION 'too late to reactivate an offboarded tenant' USING ERRCODE = 'GRD01';
  END IF;

  UPDATE public.tenants SET
    status = p_status,
    offboarded_at = CASE WHEN p_status = 'offboarded' THEN now() END,
    purge_after = CASE WHEN p_status = 'offboarded' THEN now() + interval '30 days' END
   WHERE id = p_tenant;
  IF p_status = 'suspended' THEN
    UPDATE public.refresh_tokens SET revoked_at = now(), revoked_reason = 'tenant_suspended'
     WHERE tenant_id = p_tenant AND revoked_at IS NULL;
  END IF;
  PERFORM public.audit(
    p_tenant,
    CASE WHEN public.app_user_id() IS NOT NULL AND NOT staff THEN 'user' ELSE 'system' END,
    CASE WHEN NOT staff THEN public.app_user_id() END,
    'tenant.' || p_status, 'tenant', p_tenant, NULL, NULL, p_reason, NULL, NULL, NULL, NULL);
END
$$;

CREATE FUNCTION public.expire_trials() RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  expired uuid;
  n integer := 0;
BEGIN
  FOR expired IN
    UPDATE public.tenants SET status = 'suspended'
     WHERE status = 'trial' AND trial_ends_at < now()
    RETURNING id
  LOOP
    UPDATE public.refresh_tokens SET revoked_at = now(), revoked_reason = 'tenant_suspended'
     WHERE tenant_id = expired AND revoked_at IS NULL;
    PERFORM public.audit(expired, 'system', NULL, 'tenant.suspended', 'tenant', expired, NULL,
                         NULL, 'trial_expired', NULL, NULL, NULL, NULL);
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;

GRANT EXECUTE ON FUNCTION public.set_tenant_lifecycle(uuid, text, text), public.expire_trials()
  TO app_rw;
