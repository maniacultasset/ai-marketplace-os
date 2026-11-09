-- 0024/02_proposals.up.sql (SI13): AI-drafted error-map rows for codes with no row. Platform-global
-- like marketplace_error_map (no tenant_id, no RLS, DATABASE.md "marketplace_error_map"): one row
-- per (marketplace, match_on, raw_value), holding only the code, the drafted text and soft ids.
-- Staff surfaces read it; nothing on the issue, planner, task or seller path does. A row changes
-- routing only when a platform admin approves it through the migrator CLI, which writes a
-- marketplace_error_map row. `sample_issue_id`, `agent_run_id` and `job_id` are soft references
-- (no FK): those rows are tenant rows pruned sooner (DATABASE.md "Soft references").
CREATE TABLE public.marketplace_error_proposals (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  marketplace text NOT NULL,
  match_on text NOT NULL,
  raw_value text NOT NULL,
  status text NOT NULL,
  tries smallint NOT NULL DEFAULT 0,
  next_try_at timestamptz,
  failure_reason text,
  plain_cause text,
  suggested_fix text,
  lane_class text,
  error_code text,
  error_class text,
  field_hint text,
  model_used text,
  prompt_version text,
  agent_run_id uuid,
  sample_issue_id uuid,
  job_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  reviewed_by uuid,
  reviewed_at timestamptz,
  review_note text,
  error_map_version integer,
  CONSTRAINT pk_marketplace_error_proposals PRIMARY KEY (id),
  CONSTRAINT uq_marketplace_error_proposals_key UNIQUE (marketplace, match_on, raw_value),
  CONSTRAINT ck_marketplace_error_proposals_marketplace CHECK (marketplace IN ('amazon', 'flipkart')),
  CONSTRAINT ck_marketplace_error_proposals_match_on CHECK (match_on = 'code'),
  CONSTRAINT ck_marketplace_error_proposals_raw_value CHECK (raw_value ~ '^[0-9A-Za-z_.:-]{1,128}$'),
  CONSTRAINT ck_marketplace_error_proposals_status CHECK (status IN (
    'pending', 'proposed', 'approved', 'rejected', 'invalid', 'failed')),
  CONSTRAINT ck_marketplace_error_proposals_tries CHECK (tries BETWEEN 0 AND 3),
  CONSTRAINT ck_marketplace_error_proposals_failure_reason CHECK (
    failure_reason IS NULL
    OR failure_reason IN ('not_available', 'model_failed', 'budget', 'invalid_output')),
  CONSTRAINT ck_marketplace_error_proposals_plain_cause
    CHECK (plain_cause IS NULL OR char_length(plain_cause) BETWEEN 10 AND 300),
  CONSTRAINT ck_marketplace_error_proposals_suggested_fix
    CHECK (suggested_fix IS NULL OR char_length(suggested_fix) BETWEEN 10 AND 300),
  CONSTRAINT ck_marketplace_error_proposals_lane_class
    CHECK (lane_class IS NULL OR lane_class IN ('auto', 'seller', 'marketplace')),
  CONSTRAINT ck_marketplace_error_proposals_error_code
    CHECK (error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  CONSTRAINT ck_marketplace_error_proposals_error_class
    CHECK (error_class IS NULL OR error_class ~ '^[a-z][a-z_]{0,63}$'),
  CONSTRAINT ck_marketplace_error_proposals_field_hint
    CHECK (field_hint IS NULL OR char_length(field_hint) BETWEEN 1 AND 192),
  CONSTRAINT ck_marketplace_error_proposals_model_used
    CHECK (model_used IS NULL OR char_length(model_used) BETWEEN 1 AND 128),
  CONSTRAINT ck_marketplace_error_proposals_prompt_version
    CHECK (prompt_version IS NULL OR char_length(prompt_version) BETWEEN 1 AND 64),
  CONSTRAINT ck_marketplace_error_proposals_review_note
    CHECK (review_note IS NULL OR char_length(review_note) <= 500),
  CONSTRAINT ck_marketplace_error_proposals_text CHECK (
    (status = 'proposed') = (plain_cause IS NOT NULL AND lane_class IS NOT NULL
                             AND suggested_fix IS NOT NULL)
    OR status IN ('approved', 'rejected')),
  CONSTRAINT ck_marketplace_error_proposals_no_text CHECK (
    status NOT IN ('pending', 'invalid', 'failed')
    OR (plain_cause IS NULL AND suggested_fix IS NULL AND lane_class IS NULL
        AND error_code IS NULL AND error_class IS NULL AND field_hint IS NULL)),
  CONSTRAINT ck_marketplace_error_proposals_review CHECK (
    (status IN ('approved', 'rejected')) = (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    AND (status <> 'approved' OR error_map_version IS NOT NULL))
);

CREATE INDEX ix_marketplace_error_proposals_status
  ON public.marketplace_error_proposals (status, next_try_at);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.marketplace_error_proposals
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- The worker drafts, never approves: app_rw inserts `pending` rows and moves them through the
-- drafting states (a pending row may take its job id); only the migrator (the staff CLI) moves `proposed` to approved or rejected.
-- Closed rows never change. Keyed on current_user: the staff CLI logs in as the admin and takes
-- the migrator role with SET ROLE (app.db.migrations.assume_migration_role); app_rw cannot.
CREATE FUNCTION public.marketplace_error_proposals_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF current_user <> 'migrator' AND (NEW.status <> 'pending' OR NEW.tries <> 0) THEN
      RAISE EXCEPTION 'a proposal starts pending with no tries' USING ERRCODE = 'GRD01';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status IN ('approved', 'rejected') THEN
    RAISE EXCEPTION 'a reviewed proposal never changes' USING ERRCODE = 'GRD01';
  END IF;
  IF current_user = 'migrator' THEN
    IF NOT (OLD.status = 'proposed' AND NEW.status IN ('approved', 'rejected')) THEN
      RAISE EXCEPTION 'staff review moves a proposed row to approved or rejected only'
        USING ERRCODE = 'GRD01';
    END IF;
  ELSIF NOT ((OLD.status = 'pending' AND NEW.status IN ('pending', 'proposed', 'invalid', 'failed'))
             OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'failed'))) THEN
    RAISE EXCEPTION 'proposal % -> % is not a drafting step', OLD.status, NEW.status
      USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER marketplace_error_proposals_guard
  BEFORE INSERT OR UPDATE ON public.marketplace_error_proposals
  FOR EACH ROW EXECUTE FUNCTION public.marketplace_error_proposals_guard();

REVOKE ALL ON public.marketplace_error_proposals FROM PUBLIC, app_rw, app_ro, app_platform_ro;
GRANT SELECT, INSERT ON public.marketplace_error_proposals TO app_rw;
GRANT UPDATE (status, tries, next_try_at, failure_reason, plain_cause, suggested_fix, lane_class,
              error_code, error_class, field_hint, model_used, prompt_version, agent_run_id,
              sample_issue_id, job_id, updated_at)
  ON public.marketplace_error_proposals TO app_rw;
GRANT SELECT ON public.marketplace_error_proposals TO app_platform_ro;
