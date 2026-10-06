-- 0004/06b_status_events.up.sql
-- DATABASE.md §6.1 (:1402, second half) "Event rules", §5.3 review_decisions "Order" (:805) and
-- job_runs "Statement order for a publish-family event" (:948). A second BEFORE UPDATE trigger on
-- `listings`, named to fire after `listings_status_guard`; when `status` or `status_reason`
-- changes, the event in NEW.last_event needs its evidence in this transaction (→ LST01):
--   edit                       → current_version_id moved to a new `origin = 'edit'` version;
--   approve, reject,
--   reject_regenerate,
--   regenerate                 → a review_decisions row (approve / reject / reject /
--                                regenerate_requested) for current_version_id, decided now()
--                                by app.user_id (now() is the transaction's timestamp);
--   generate, regenerate,
--   reject_regenerate          → a queued listing_generate job of the listing;
--   publish, retry_publish,
--   auto_resubmit,
--   resume_publish             → a queued listing_publish job, and fewer than 3 submissions for
--                                the approved version (so INSERT job_runs comes first);
--   submission_accepted        → published_version_id is an accepted submission's version.
CREATE FUNCTION public.listings_status_events() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  e text := NEW.last_event;
  needed text;
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status
     AND NEW.status_reason IS NOT DISTINCT FROM OLD.status_reason THEN
    RETURN NEW;
  END IF;

  IF e = 'edit' AND NOT (
       NEW.current_version_id IS DISTINCT FROM OLD.current_version_id
       AND EXISTS (SELECT 1 FROM public.listing_versions v
                    WHERE v.listing_id = NEW.id AND v.id = NEW.current_version_id
                      AND v.origin = 'edit')) THEN
    RAISE EXCEPTION 'edit needs a new edit version as current' USING ERRCODE = 'LST01';
  END IF;

  needed := CASE e WHEN 'approve' THEN 'approve' WHEN 'reject' THEN 'reject'
                   WHEN 'reject_regenerate' THEN 'reject'
                   WHEN 'regenerate' THEN 'regenerate_requested' END;
  IF needed IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM public.review_decisions d
        WHERE d.tenant_id = NEW.tenant_id AND d.listing_id = NEW.id
          AND d.listing_version_id IS NOT DISTINCT FROM NEW.current_version_id
          AND d.decision = needed
          AND d.decided_at = now() AND d.decided_by_user_id = public.app_user_id()) THEN
    RAISE EXCEPTION '% needs a % decision on the current version in this transaction', e, needed
      USING ERRCODE = 'LST01';
  END IF;

  IF e IN ('generate', 'regenerate', 'reject_regenerate') AND NOT EXISTS (
       SELECT 1 FROM public.job_runs j
        WHERE j.tenant_id = NEW.tenant_id AND j.listing_id = NEW.id
          AND j.kind = 'listing_generate' AND j.status = 'queued') THEN
    RAISE EXCEPTION '% needs a queued listing_generate job', e USING ERRCODE = 'LST01';
  END IF;

  IF e IN ('publish', 'retry_publish', 'auto_resubmit', 'resume_publish') THEN
    IF NOT EXISTS (
         SELECT 1 FROM public.job_runs j
          WHERE j.tenant_id = NEW.tenant_id AND j.listing_id = NEW.id
            AND j.kind = 'listing_publish' AND j.status = 'queued') THEN
      RAISE EXCEPTION '% needs a queued listing_publish job (insert it first)', e
        USING ERRCODE = 'LST01';
    END IF;
    IF (SELECT count(*) FROM public.listing_submissions s
         WHERE s.listing_id = NEW.id AND s.listing_version_id = NEW.approved_version_id) >= 3 THEN
      RAISE EXCEPTION '% after 3 submissions of the version', e USING ERRCODE = 'LST01';
    END IF;
  END IF;

  IF e = 'submission_accepted' AND NOT EXISTS (
       SELECT 1 FROM public.listing_submissions s
        WHERE s.listing_id = NEW.id AND s.listing_version_id = NEW.published_version_id
          AND s.outcome = 'accepted') THEN
    RAISE EXCEPTION 'submission_accepted needs published_version_id = an accepted submission''s version'
      USING ERRCODE = 'LST01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER listings_status_guard_events BEFORE UPDATE ON public.listings
  FOR EACH ROW EXECUTE FUNCTION public.listings_status_events();
