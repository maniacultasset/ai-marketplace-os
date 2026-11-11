-- 0033/02_agent_run_reason.up.sql (client feedback 14): why a generation job's model calls ran
-- and what they could change. `run_reason`: `generate` (the first listing), `regenerate` (the
-- whole listing again), `section_regenerate` (the seller's sections), `answers_applied` (the
-- sections the seller's answers feed) or `resolution` (the resolution engine's AI rewrite);
-- NULL for a model call outside generation. `run_scope`: the canonical paths the run could
-- change; NULL for the whole listing. Nullable, no default: no table rewrite, and the
-- append-only rows written before stay as they are.
ALTER TABLE public.agent_runs
  ADD COLUMN run_reason text,
  ADD COLUMN run_scope text[];
ALTER TABLE public.agent_runs
  ADD CONSTRAINT ck_agent_runs_run_reason CHECK (
    run_reason IS NULL
    OR run_reason IN ('generate', 'regenerate', 'section_regenerate', 'answers_applied',
                      'resolution')
  ) NOT VALID;
ALTER TABLE public.agent_runs VALIDATE CONSTRAINT ck_agent_runs_run_reason;
ALTER TABLE public.agent_runs
  ADD CONSTRAINT ck_agent_runs_run_scope CHECK (
    run_scope IS NULL OR cardinality(run_scope) BETWEEN 1 AND 16
  ) NOT VALID;
ALTER TABLE public.agent_runs VALIDATE CONSTRAINT ck_agent_runs_run_scope;
