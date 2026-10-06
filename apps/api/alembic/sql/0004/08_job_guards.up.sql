-- 0004/08_job_guards.up.sql
-- DATABASE.md §5.4 job_runs "Triggers" (:947) and §6.2 (:1465-1487): `status` moves only along the
-- JobState edges (the claim is queued → running; the reaper's are running → queued | dead_letter),
-- and a terminal row (succeeded, partial, failed, cancelled, dead_letter) is immutable: a retry or
-- requeue is a new row with parent_job_id. DELETE stays allowed (the pruner). Refusals → GRD01.
CREATE FUNCTION public.job_runs_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF OLD.status IN ('succeeded', 'partial', 'failed', 'cancelled', 'dead_letter') THEN
    RAISE EXCEPTION 'job_runs row % is terminal (%)', OLD.id, OLD.status USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND (OLD.status, NEW.status) NOT IN (
       ('queued', 'running'), ('queued', 'cancelled'),
       ('running', 'succeeded'), ('running', 'partial'), ('running', 'queued'),
       ('running', 'failed'), ('running', 'dead_letter'), ('running', 'cancelled')) THEN
    RAISE EXCEPTION 'job_runs status % -> % is not an edge', OLD.status, NEW.status
      USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER job_runs_guard BEFORE UPDATE ON public.job_runs
  FOR EACH ROW EXECUTE FUNCTION public.job_runs_guard();
