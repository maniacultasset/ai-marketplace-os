-- 0008/ls_guard.down.sql
-- Restores the parent revision's ls_guard() (0004/10_ls_guard.up.sql, unchanged through
-- 0007_facts_guard) verbatim: retryable_condition accepts only a previous retryable `error`.
CREATE OR REPLACE FUNCTION public.ls_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  l public.listings%ROWTYPE;
  prev public.listing_submissions%ROWTYPE;
  approved_digest bytea;
  sent int;
  failed_fields text[];
  ok boolean;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.outcome IN ('partially_accepted', 'rejected', 'error', 'live', 'superseded') THEN
      RAISE EXCEPTION 'submission % is terminal (%)', OLD.id, OLD.outcome USING ERRCODE = 'SUB03';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['outcome', 'error_code', 'error_report_json', 'submission_ref',
                              'raw_response_sha256', 'raw_response_storage_key', 'sent_at',
                              'responded_at', 'resolved_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['outcome', 'error_code', 'error_report_json', 'submission_ref',
                              'raw_response_sha256', 'raw_response_storage_key', 'sent_at',
                              'responded_at', 'resolved_at']) THEN
      RAISE EXCEPTION 'only the outcome columns of a submission change' USING ERRCODE = 'SUB03';
    END IF;
    IF NEW.outcome IS DISTINCT FROM OLD.outcome AND NOT (
         (OLD.outcome = 'pending'
          AND NEW.outcome IN ('accepted', 'partially_accepted', 'rejected', 'error', 'live'))
         OR (OLD.outcome = 'accepted'
             AND NEW.outcome IN ('live', 'partially_accepted', 'rejected', 'error', 'superseded'))) THEN
      RAISE EXCEPTION 'submission outcome % -> % refused', OLD.outcome, NEW.outcome
        USING ERRCODE = 'SUB03';
    END IF;
    IF OLD.outcome = 'accepted' AND NEW.outcome = 'error'
       AND NEW.error_code IS DISTINCT FROM 'MARKETPLACE_UNCONFIRMED' THEN
      RAISE EXCEPTION 'accepted -> error only as MARKETPLACE_UNCONFIRMED' USING ERRCODE = 'SUB03';
    END IF;
    IF NEW.outcome = 'superseded' AND OLD.outcome <> 'superseded' AND NOT EXISTS (
         SELECT 1 FROM public.listings x
          WHERE x.tenant_id = NEW.tenant_id AND x.id = NEW.listing_id
            AND x.approved_version_id IS DISTINCT FROM NEW.listing_version_id
            AND x.approved_version_id IS NOT NULL) THEN
      RAISE EXCEPTION 'superseded needs a newer approved version' USING ERRCODE = 'SUB03';
    END IF;
    IF NEW.outcome = 'error' AND OLD.outcome <> 'error' AND NEW.sent_at IS NOT NULL
       AND NEW.error_code IS DISTINCT FROM 'SUBMISSION_NOT_APPLIED'
       AND NEW.error_code IS DISTINCT FROM 'MARKETPLACE_UNCONFIRMED'
       AND NOT coalesce(NEW.error_code IN ('RATE_LIMITED', 'AUTH_EXPIRED',
                                           'ACCOUNT_PERMISSION_DENIED'), false) THEN
      RAISE EXCEPTION 'an error after send needs a resolving read or a definitive failure'
        USING ERRCODE = 'SUB03';
    END IF;
    RETURN NEW;
  END IF;

  SELECT * INTO l FROM public.listings x
   WHERE x.tenant_id = NEW.tenant_id AND x.id = NEW.listing_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN NEW;  -- fk_listing_submissions_listing_id refuses
  END IF;
  IF l.status <> 'publishing' THEN
    RAISE EXCEPTION 'a submission needs the listing publishing (is %)', l.status
      USING ERRCODE = 'GRD01';
  END IF;
  IF l.paused_at IS NOT NULL OR l.archived_at IS NOT NULL THEN
    RAISE EXCEPTION 'a submission of a paused or archived listing' USING ERRCODE = 'GRD01';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.tenants t WHERE t.id = l.tenant_id
                    AND t.status IN ('trial', 'active')) THEN
    RAISE EXCEPTION 'the tenant is not trial or active' USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.listing_version_id IS DISTINCT FROM l.approved_version_id
     OR l.approved_version_id IS DISTINCT FROM l.current_version_id
     OR NOT EXISTS (SELECT 1 FROM public.listing_versions v
                     WHERE v.listing_id = l.id AND v.id = NEW.listing_version_id
                       AND v.approved_by_user_id IS NOT NULL) THEN
    RAISE EXCEPTION 'a submission sends the current, human-approved version'
      USING ERRCODE = 'GRD01';
  END IF;
  SELECT d.facts_digest INTO approved_digest FROM public.review_decisions d
   WHERE d.tenant_id = NEW.tenant_id AND d.listing_version_id = NEW.listing_version_id
     AND d.decision = 'approve';
  IF approved_digest IS DISTINCT FROM NEW.facts_digest THEN
    RAISE EXCEPTION 'the facts changed since the approval' USING ERRCODE = 'SUB04';
  END IF;

  SELECT count(*) INTO sent FROM public.listing_submissions s
   WHERE s.listing_id = NEW.listing_id AND s.listing_version_id = NEW.listing_version_id;
  IF sent >= 3 THEN
    RAISE EXCEPTION 'the version has 3 submissions' USING ERRCODE = 'SUB02';
  END IF;
  IF NEW.attempt <> sent + 1 THEN
    RAISE EXCEPTION 'attempt % but % earlier submissions', NEW.attempt, sent USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.attempt = 1 THEN
    RETURN NEW;
  END IF;

  SELECT * INTO prev FROM public.listing_submissions s
   WHERE s.id = NEW.previous_submission_id AND s.listing_id = NEW.listing_id
     AND s.listing_version_id = NEW.listing_version_id;
  IF NOT FOUND OR prev.attempt <> NEW.attempt - 1 OR prev.resolved_at IS NULL
     OR prev.outcome NOT IN ('partially_accepted', 'rejected', 'error') THEN
    RAISE EXCEPTION 'the previous attempt must be the last one, resolved and not accepted'
      USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.actor_type = 'system' AND NEW.requested_by_user_id IS DISTINCT FROM prev.requested_by_user_id
  THEN
    RAISE EXCEPTION 'a system resubmission keeps the requester' USING ERRCODE = 'SUB01';
  END IF;

  ok := false;
  IF NEW.retry_justification = 'changed_input' THEN
    SELECT array_agg(DISTINCT r.field) INTO failed_fields FROM public.publish_field_results r
     WHERE r.submission_id = prev.id AND r.status = 'failed';
    ok := NEW.payload_sha256 IS DISTINCT FROM prev.payload_sha256
      AND jsonb_typeof(NEW.corrections_json) = 'array'
      AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(NEW.corrections_json) c
         WHERE (c ->> 'to_digest') IS DISTINCT FROM (c ->> 'from_digest')
           AND (failed_fields IS NULL OR (c ->> 'field') = ANY (failed_fields)));
  ELSIF NEW.retry_justification = 'retryable_condition' THEN
    ok := prev.outcome = 'error'
      AND coalesce((prev.error_report_json ->> 'retryable')::boolean, false);
  ELSIF NEW.retry_justification = 'marketplace_transition' THEN
    ok := l.marketplace_state_changed_at > prev.resolved_at;
  ELSIF NEW.retry_justification = 'human_resolution' THEN
    ok := EXISTS (
      SELECT 1 FROM public.listing_issues i JOIN public.human_tasks t
        ON t.tenant_id = i.tenant_id AND t.id = i.human_task_id
       WHERE i.tenant_id = NEW.tenant_id AND i.job_id = prev.job_id
         AND t.status = 'applied' AND t.applied_at > prev.resolved_at);
  END IF;
  IF NOT coalesce(ok, false) THEN
    RAISE EXCEPTION 'resubmission not justified (%)', NEW.retry_justification
      USING ERRCODE = 'SUB01';
  END IF;
  RETURN NEW;
END
$$;
