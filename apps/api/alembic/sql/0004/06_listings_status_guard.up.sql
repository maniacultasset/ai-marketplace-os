-- 0004/06_listings_status_guard.up.sql
-- DATABASE.md §5.3 listings "Guard listings_status_guard" (:767) and §6.1 (:1402), first half: the
-- table-driven core. When `status` or `status_reason` changes:
--   * a paused or archived listing changes no status → LST02, CONSTRAINT 'paused' | 'archived'
--     (→ 409 LISTING_PAUSED / LISTING_ARCHIVED);
--   * (OLD.status, NEW.status, NEW.last_event) must be a `listing_status_transitions` row, whose
--     actor needs its GUC (`user` → app.user_id, `worker` → app.job_id), approval flags and
--     reasons must hold → LST01 (→ 409 INVALID_STATUS_TRANSITION);
--   * `approved` and `publishing` need approved_version_id = current_version_id carrying a human
--     approval, and the tenant `trial` or `active` (invariants 1 and 3);
--   * `status_changed_at` and `last_event_at` are stamped.
-- On every write: the listing's product is not a variant child, and `marketplace_sku` and
-- `external_id`, once set, change only with app.job_id set → GRD01. The event rules are 06b.
CREATE FUNCTION public.listings_status_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  edge public.listing_status_transitions%ROWTYPE;
  approved_current boolean;
BEGIN
  IF TG_OP = 'INSERT' OR NEW.product_id IS DISTINCT FROM OLD.product_id THEN
    IF EXISTS (SELECT 1 FROM public.products p
                WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.product_id
                  AND p.parent_product_id IS NOT NULL) THEN
      RAISE EXCEPTION 'a listing is of a style or standalone product, never a variant child'
        USING ERRCODE = 'GRD01';
    END IF;
  END IF;
  IF TG_OP = 'INSERT' THEN
    RETURN NEW;
  END IF;

  IF (OLD.marketplace_sku IS NOT NULL AND NEW.marketplace_sku IS DISTINCT FROM OLD.marketplace_sku
      OR OLD.external_id IS NOT NULL AND NEW.external_id IS DISTINCT FROM OLD.external_id)
     AND public.app_job_id() IS NULL THEN
    RAISE EXCEPTION 'marketplace_sku and external_id change only from a job' USING ERRCODE = 'GRD01';
  END IF;

  IF NEW.status IS NOT DISTINCT FROM OLD.status
     AND NEW.status_reason IS NOT DISTINCT FROM OLD.status_reason THEN
    RETURN NEW;
  END IF;

  IF OLD.paused_at IS NOT NULL OR NEW.paused_at IS NOT NULL THEN
    RAISE EXCEPTION 'listing is paused' USING ERRCODE = 'LST02', CONSTRAINT = 'paused';
  END IF;
  IF OLD.archived_at IS NOT NULL OR NEW.archived_at IS NOT NULL THEN
    RAISE EXCEPTION 'listing is archived' USING ERRCODE = 'LST02', CONSTRAINT = 'archived';
  END IF;

  SELECT * INTO edge FROM public.listing_status_transitions t
   WHERE t.from_status = OLD.status AND t.to_status = NEW.status AND t.event = NEW.last_event;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no transition % -> % on %', OLD.status, NEW.status, NEW.last_event
      USING ERRCODE = 'LST01';
  END IF;
  IF edge.actor = 'user' AND public.app_user_id() IS NULL THEN
    RAISE EXCEPTION '% needs a user (app.user_id)', NEW.last_event USING ERRCODE = 'LST01';
  END IF;
  IF edge.actor = 'worker' AND public.app_job_id() IS NULL THEN
    RAISE EXCEPTION '% needs a job (app.job_id)', NEW.last_event USING ERRCODE = 'LST01';
  END IF;

  approved_current := NEW.approved_version_id IS NOT NULL
    AND NEW.approved_version_id = NEW.current_version_id
    AND EXISTS (SELECT 1 FROM public.listing_versions v
                 WHERE v.listing_id = NEW.id AND v.id = NEW.approved_version_id
                   AND v.approved_by_user_id IS NOT NULL);
  IF (edge.requires_current_approval OR NEW.status IN ('approved', 'publishing'))
     AND NOT approved_current THEN
    RAISE EXCEPTION '% needs a current human approval', NEW.last_event USING ERRCODE = 'LST01';
  END IF;
  IF edge.voids_approval AND NEW.approved_version_id IS NOT NULL
     AND NEW.approved_version_id = NEW.current_version_id THEN
    RAISE EXCEPTION '% must void the approval', NEW.last_event USING ERRCODE = 'LST01';
  END IF;
  IF edge.allowed_reasons IS NOT NULL
     AND NOT coalesce(NEW.status_reason = ANY (edge.allowed_reasons), false) THEN
    RAISE EXCEPTION 'reason % not allowed for %', NEW.status_reason, NEW.last_event
      USING ERRCODE = 'LST01';
  END IF;
  IF edge.requires_from_reason IS NOT NULL
     AND NOT coalesce(OLD.status_reason = ANY (edge.requires_from_reason), false) THEN
    RAISE EXCEPTION '% needs the listing in reason %', NEW.last_event, edge.requires_from_reason
      USING ERRCODE = 'LST01';
  END IF;
  IF NEW.status IN ('approved', 'publishing') AND NOT EXISTS (
       SELECT 1 FROM public.tenants t WHERE t.id = NEW.tenant_id AND t.status IN ('trial', 'active'))
  THEN
    RAISE EXCEPTION 'the tenant is not trial or active' USING ERRCODE = 'LST01';
  END IF;

  NEW.status_changed_at := now();
  NEW.last_event_at := now();
  RETURN NEW;
END
$$;

CREATE TRIGGER listings_status_guard BEFORE INSERT OR UPDATE ON public.listings
  FOR EACH ROW EXECUTE FUNCTION public.listings_status_guard();
