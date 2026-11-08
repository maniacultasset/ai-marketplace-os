-- 0022/01_status.up.sql
-- SI11 (autonomous listing resolution, ADR-0028 proposed). Expand only, in one transaction:
-- 1. the event vocabulary (ck_listing_status_transitions_event, ck_listings_last_event) gains
--    'auto_fix': the resolution engine's content fix, run by a listing_generate job;
-- 2. 9 worker edges: auto_fix -> generating (from validated, needs_review, approved and published,
--    the last two voiding the approval; from failed after a publish failure or a rejection; from
--    blocked/validation_errors) and corrections_exhausted -> blocked (the 3-cycle cap, invariant 9)
--    from validated, needs_review and failed;
-- 3. listings_status_events(): auto_fix, like generate, needs a queued listing_generate job;
-- 4. listings.remote_check_version_id / remote_check_status / remote_check_at: the marketplace's
--    dry-run verdict (VALIDATION_PREVIEW) on one version (pending, clean, issues, failed).
-- squawk constraint-missing-not-valid (NOT VALID and VALIDATE in one transaction): accepted as in
-- 0014/0015/0020. Both tables are small in v1, and one transaction keeps each swap atomic.
ALTER TABLE public.listing_status_transitions
  DROP CONSTRAINT ck_listing_status_transitions_event,
  ADD CONSTRAINT ck_listing_status_transitions_event CHECK (event IN (
    'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
    'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
    'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
    'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
    'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
    'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive',
    'auto_fix')) NOT VALID;
ALTER TABLE public.listing_status_transitions VALIDATE CONSTRAINT ck_listing_status_transitions_event;
ALTER TABLE public.listings
  DROP CONSTRAINT ck_listings_last_event,
  ADD CONSTRAINT ck_listings_last_event CHECK (last_event IN (
    'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
    'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
    'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
    'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
    'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
    'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive',
    'auto_fix')) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_listings_last_event;

INSERT INTO public.listing_status_transitions
  (from_status, to_status, event, actor, requires_current_approval, voids_approval,
   allowed_reasons, requires_from_reason)
VALUES
  ('validated', 'generating', 'auto_fix', 'worker', false, false, NULL, NULL),
  ('needs_review', 'generating', 'auto_fix', 'worker', false, false, NULL, NULL),
  ('approved', 'generating', 'auto_fix', 'worker', false, true, NULL, NULL),
  ('published', 'generating', 'auto_fix', 'worker', false, true, NULL, NULL),
  ('failed', 'generating', 'auto_fix', 'worker', false, false, NULL,
   '{publish_failed,publish_partial,marketplace_rejected}'),
  ('blocked', 'generating', 'auto_fix', 'worker', false, false, NULL, '{validation_errors}'),
  ('validated', 'blocked', 'corrections_exhausted', 'worker', false, false, '{corrections_exhausted}', NULL),
  ('needs_review', 'blocked', 'corrections_exhausted', 'worker', false, false, '{corrections_exhausted}', NULL),
  ('failed', 'blocked', 'corrections_exhausted', 'worker', false, false, '{corrections_exhausted}', NULL);

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

  IF e IN ('generate', 'regenerate', 'reject_regenerate', 'auto_fix') AND NOT EXISTS (
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

ALTER TABLE public.listings
  ADD COLUMN remote_check_version_id uuid,
  ADD COLUMN remote_check_status text,
  ADD COLUMN remote_check_at timestamptz;
ALTER TABLE public.listings
  ADD CONSTRAINT ck_listings_remote_check CHECK (
    (remote_check_version_id IS NULL) = (remote_check_status IS NULL)
    AND (remote_check_status IS NULL
         OR remote_check_status IN ('pending', 'clean', 'issues', 'failed'))
    AND (remote_check_status IS NULL OR remote_check_status = 'pending'
         OR remote_check_at IS NOT NULL)
  ) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_listings_remote_check;
-- the purge defers it, as the other version pointers (0002)
ALTER TABLE public.listings
  ADD CONSTRAINT fk_listings_remote_check_version_id
  FOREIGN KEY (id, remote_check_version_id) REFERENCES public.listing_versions (listing_id, id)
  DEFERRABLE INITIALLY IMMEDIATE NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT fk_listings_remote_check_version_id;
