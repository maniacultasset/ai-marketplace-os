-- 0004/05_last_owner.up.sql
-- DATABASE.md §5.1 tenant_memberships "Last-owner trigger" (:256): deferred to commit, so a
-- transaction may demote one owner and promote another in any order. When a row that was an
-- active owner stops being one (demoted, deactivated or deleted), it takes
-- pg_advisory_xact_lock(hashtext('owners:' || tenant_id)) before counting, so two owners demoting
-- each other cannot both commit (write skew), and refuses (OWN01 → 409 LAST_OWNER_REQUIRED) when
-- the tenant still exists, is not offboarded, and has no active owner left. `SET CONSTRAINTS
-- last_owner_guard IMMEDIATE` makes it raise at the statement.
CREATE FUNCTION public.last_owner_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF NOT (OLD.role = 'owner' AND OLD.is_active) THEN
    RETURN NULL;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.role = 'owner' AND NEW.is_active THEN
    RETURN NULL;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('owners:' || OLD.tenant_id::text));
  IF EXISTS (SELECT 1 FROM public.tenants t WHERE t.id = OLD.tenant_id AND t.status <> 'offboarded')
     AND NOT EXISTS (SELECT 1 FROM public.tenant_memberships m
                      WHERE m.tenant_id = OLD.tenant_id AND m.role = 'owner' AND m.is_active) THEN
    RAISE EXCEPTION 'a tenant needs an active owner' USING ERRCODE = 'OWN01';
  END IF;
  RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER last_owner_guard AFTER UPDATE OR DELETE ON public.tenant_memberships
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.last_owner_guard();
