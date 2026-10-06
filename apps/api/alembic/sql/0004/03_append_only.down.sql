-- 0004/03_append_only.down.sql
-- Back to the 0000 default privileges these tables were created with.
GRANT UPDATE, DELETE ON public.audit_log, public.agent_runs, public.validation_runs,
  public.review_decisions TO app_rw;
DROP TRIGGER append_only_truncate ON public.review_decisions;
DROP TRIGGER append_only_guard ON public.review_decisions;
DROP TRIGGER append_only_truncate ON public.validation_runs;
DROP TRIGGER append_only_guard ON public.validation_runs;
DROP TRIGGER append_only_truncate ON public.agent_runs;
DROP TRIGGER append_only_guard ON public.agent_runs;
DROP TRIGGER append_only_truncate ON public.audit_log;
DROP TRIGGER append_only_guard ON public.audit_log;
DROP FUNCTION public.append_only_guard();
