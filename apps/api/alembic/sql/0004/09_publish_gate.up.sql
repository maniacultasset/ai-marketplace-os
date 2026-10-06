-- 0004/09_publish_gate.up.sql
-- DATABASE.md §5.4 job_runs "Publish gate" (:947): BEFORE INSERT of a `listing_publish` row.
--   * the listing has approved_version_id = current_version_id and status `approved`, `failed`
--     (retry) or `blocked` with reason `marketplace_action_required` (resume); not paused or
--     archived; fewer than 3 submissions for the approved version;
--   * the marketplace account is `active`: the listing's own account once set, else (before the
--     first submission sets it) an active account of the tenant for the listing's marketplace;
--   * no open `error` issue with retry_class `user_action` first seen on the approved version that
--     is not linked to an `answered` or `applied` task (anti-loop, T71);
--   * `created_by` is an active `owner` or `operator`, read FOR SHARE. A NULL `created_by` is left
--     to ck_job_runs_no_auto_publish.
-- Refusals → GRD01. The status guard (06b) then needs this queued job, so a publish writes INSERT
-- job_runs before UPDATE listings.
CREATE FUNCTION public.publish_gate() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  l public.listings%ROWTYPE;
  m record;
BEGIN
  IF NEW.kind <> 'listing_publish' OR NEW.created_by IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT * INTO l FROM public.listings x WHERE x.tenant_id = NEW.tenant_id AND x.id = NEW.listing_id;
  IF NOT FOUND THEN
    RETURN NEW;  -- fk_job_runs_listing_id refuses
  END IF;
  IF l.approved_version_id IS NULL OR l.approved_version_id IS DISTINCT FROM l.current_version_id
     OR NOT EXISTS (SELECT 1 FROM public.listing_versions v
                     WHERE v.listing_id = l.id AND v.id = l.approved_version_id
                       AND v.approved_by_user_id IS NOT NULL) THEN
    RAISE EXCEPTION 'publish needs the current version approved' USING ERRCODE = 'GRD01';
  END IF;
  IF NOT (l.status IN ('approved', 'failed')
          OR (l.status = 'blocked' AND l.status_reason = 'marketplace_action_required')) THEN
    RAISE EXCEPTION 'publish from status %/% refused', l.status, l.status_reason
      USING ERRCODE = 'GRD01';
  END IF;
  IF l.paused_at IS NOT NULL OR l.archived_at IS NOT NULL THEN
    RAISE EXCEPTION 'publish of a paused or archived listing refused' USING ERRCODE = 'GRD01';
  END IF;
  IF (SELECT count(*) FROM public.listing_submissions s
       WHERE s.listing_id = l.id AND s.listing_version_id = l.approved_version_id) >= 3 THEN
    RAISE EXCEPTION 'the approved version has 3 submissions' USING ERRCODE = 'GRD01';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.marketplace_accounts a
                  WHERE a.tenant_id = l.tenant_id AND a.marketplace = l.marketplace
                    AND a.status = 'active'
                    AND (l.marketplace_account_id IS NULL OR a.id = l.marketplace_account_id)) THEN
    RAISE EXCEPTION 'publish needs an active marketplace account' USING ERRCODE = 'GRD01';
  END IF;
  IF EXISTS (SELECT 1 FROM public.listing_issues i
              WHERE i.tenant_id = l.tenant_id AND i.listing_id = l.id AND i.status = 'open'
                AND i.severity = 'error' AND i.retry_class = 'user_action'
                AND i.listing_version_id = l.approved_version_id
                AND NOT EXISTS (SELECT 1 FROM public.human_tasks t
                                 WHERE t.tenant_id = i.tenant_id AND t.id = i.human_task_id
                                   AND t.status IN ('answered', 'applied'))) THEN
    RAISE EXCEPTION 'an open user-action issue blocks the publish' USING ERRCODE = 'GRD01';
  END IF;
  SELECT tm.role, tm.is_active INTO m FROM public.tenant_memberships tm
   WHERE tm.tenant_id = NEW.tenant_id AND tm.user_id = NEW.created_by FOR SHARE;
  IF FOUND AND NOT (m.is_active AND m.role IN ('owner', 'operator')) THEN
    RAISE EXCEPTION 'a publish needs an active owner or operator' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER publish_gate BEFORE INSERT ON public.job_runs
  FOR EACH ROW EXECUTE FUNCTION public.publish_gate();
