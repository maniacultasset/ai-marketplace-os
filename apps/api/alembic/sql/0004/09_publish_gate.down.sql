-- 0004/09_publish_gate.down.sql
DROP TRIGGER publish_gate ON public.job_runs;
DROP FUNCTION public.publish_gate();
