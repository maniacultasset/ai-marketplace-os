-- 0022/01_status.down.sql (local and CI only, RULES.md "No prod downgrade")
-- Fails loud while a listing's last event is auto_fix: the old CHECK cannot hold it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.listings WHERE last_event = 'auto_fix') THEN
    RAISE EXCEPTION 'downgrade refused: a listing has last_event auto_fix'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;

ALTER TABLE public.listings
  DROP CONSTRAINT fk_listings_remote_check_version_id,
  DROP CONSTRAINT ck_listings_remote_check,
  DROP COLUMN remote_check_at,
  DROP COLUMN remote_check_status,
  DROP COLUMN remote_check_version_id;

-- the 0004/06b body
CREATE OR REPLACE FUNCTION public.listings_status_events() RETURNS trigger
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

DELETE FROM public.listing_status_transitions
 WHERE event = 'auto_fix'
    OR (event = 'corrections_exhausted' AND from_status IN ('validated', 'needs_review', 'failed'));

ALTER TABLE public.listing_status_transitions
  DROP CONSTRAINT ck_listing_status_transitions_event,
  ADD CONSTRAINT ck_listing_status_transitions_event CHECK (event IN (
    'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
    'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
    'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
    'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
    'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
    'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive')) NOT VALID;
ALTER TABLE public.listing_status_transitions VALIDATE CONSTRAINT ck_listing_status_transitions_event;
ALTER TABLE public.listings
  DROP CONSTRAINT ck_listings_last_event,
  ADD CONSTRAINT ck_listings_last_event CHECK (last_event IN (
    'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
    'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
    'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
    'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
    'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
    'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive')) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_listings_last_event;
