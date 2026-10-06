-- 0004/03_append_only.up.sql
-- DATABASE.md §5.2 audit_log "Append-only", §9 "Pruning" (:1847-1849) and the purge (:1871-1874).
-- `audit_log`, `agent_runs`, `validation_runs` and `review_decisions` take INSERT and SELECT only:
-- the REVOKE removes what the 0000 default privileges grant `app_rw`, and the trigger refuses an
-- UPDATE, DELETE or TRUNCATE by anyone, a superuser included, unless the session user is
-- `migrator` (inside a SECURITY DEFINER function `current_user` is the owner, so it is never
-- checked) and a matching GUC is set:
--   app.purge_tenant = the row's tenant_id (purge_tenant: deletes; nulls audit email and ip),
--   app.prune_table  = this table (prune_*: deletes only),
--   app.redact_user  = the audit row's actor_user_id (redact_user: audit_log updates only).
-- An allowed UPDATE is only ever on audit_log, and only sets actor_email and ip to NULL: every
-- other column must be unchanged, so neither GUC can forge or rewrite an audit row.
-- TRUNCATE has no row to match, so it is always refused.
CREATE FUNCTION public.append_only_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  purge uuid := nullif(current_setting('app.purge_tenant', true), '')::uuid;
  prune text := nullif(current_setting('app.prune_table', true), '');
  redact uuid := nullif(current_setting('app.redact_user', true), '')::uuid;
  null_out boolean;
BEGIN
  IF TG_OP <> 'TRUNCATE' AND session_user = 'migrator' THEN
    null_out := TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'audit_log'
      AND to_jsonb(NEW) ->> 'actor_email' IS NULL AND to_jsonb(NEW) ->> 'ip' IS NULL
      AND to_jsonb(NEW) - 'actor_email' - 'ip' = to_jsonb(OLD) - 'actor_email' - 'ip';
    IF purge IS NOT NULL AND OLD.tenant_id = purge AND (TG_OP = 'DELETE' OR null_out) THEN
      RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'DELETE' AND prune IS NOT NULL AND prune::regclass = TG_RELID THEN
      RETURN OLD;
    END IF;
    IF null_out AND redact IS NOT NULL
       AND (to_jsonb(OLD) ->> 'actor_user_id')::uuid = redact THEN
      RETURN NEW;
    END IF;
  END IF;
  RAISE EXCEPTION '% on append-only table % refused', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'GRD01';
END
$$;

CREATE TRIGGER append_only_guard BEFORE UPDATE OR DELETE ON public.audit_log
  FOR EACH ROW EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON public.audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_guard BEFORE UPDATE OR DELETE ON public.agent_runs
  FOR EACH ROW EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON public.agent_runs
  FOR EACH STATEMENT EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_guard BEFORE UPDATE OR DELETE ON public.validation_runs
  FOR EACH ROW EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON public.validation_runs
  FOR EACH STATEMENT EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_guard BEFORE UPDATE OR DELETE ON public.review_decisions
  FOR EACH ROW EXECUTE FUNCTION public.append_only_guard();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON public.review_decisions
  FOR EACH STATEMENT EXECUTE FUNCTION public.append_only_guard();

REVOKE UPDATE, DELETE, TRUNCATE ON public.audit_log, public.agent_runs, public.validation_runs,
  public.review_decisions FROM app_rw;
