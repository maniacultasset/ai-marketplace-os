-- 0036/02_agent_run_reason.down.sql (local and CI only)
ALTER TABLE public.agent_runs DROP CONSTRAINT ck_agent_runs_run_scope;
ALTER TABLE public.agent_runs DROP CONSTRAINT ck_agent_runs_run_reason;
ALTER TABLE public.agent_runs DROP COLUMN run_scope, DROP COLUMN run_reason;
