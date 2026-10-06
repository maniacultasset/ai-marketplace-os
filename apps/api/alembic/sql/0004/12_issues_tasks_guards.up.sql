-- 0004/12_issues_tasks_guards.up.sql
-- DATABASE.md §5.7 listing_issues "listing_issues_guard" (:1326-1328) and human_tasks
-- "human_tasks_guard" (:1355); §9 "Pruning" (closed issues and terminal tasks are pruned only by
-- the migrator under the prune GUC, or deleted by the purge). Refusals → GRD01.
-- listing_issues:
--   * rule_id names a platform rule or a rule of the row's tenant (the plain-FK exception, §5.2);
--   * status moves only open → auto_fixed | resolved | dismissed; a closed row is immutable;
--     the identity columns never change;
--   * a dismissal reads the resolver's membership FOR SHARE and needs an active reviewer or
--     owner; an `error` is never dismissed (the Rules Engine stays the arbiter).
-- human_tasks:
--   * open → answered | cancelled, answered → applied | cancelled; terminal rows (applied,
--     cancelled) are immutable; kind, targets, question, reason and expected_answer never change;
--     the answer is set once.
-- A closed issue or a terminal task is deleted only by the migrator under app.prune_table (this
-- table) or app.purge_tenant (its tenant). app_rw may not DELETE either table.
CREATE FUNCTION public.listing_issues_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  m record;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'open' AND NOT coalesce(
         session_user = 'migrator'
         AND (nullif(current_setting('app.prune_table', true), '')::regclass = TG_RELID
              OR nullif(current_setting('app.purge_tenant', true), '')::uuid = OLD.tenant_id),
         false) THEN  -- an unset GUC compares NULL: refuse, never allow
      RAISE EXCEPTION 'a closed issue is deleted only by the prune or the purge'
        USING ERRCODE = 'GRD01';
    END IF;
    RETURN OLD;
  END IF;

  IF NEW.rule_id IS NOT NULL AND (TG_OP = 'INSERT' OR NEW.rule_id IS DISTINCT FROM OLD.rule_id)
     AND EXISTS (SELECT 1 FROM public.rules r  -- a missing rule is left to the plain FK (23503)
                  WHERE r.id = NEW.rule_id AND r.tenant_id IS DISTINCT FROM NEW.tenant_id
                    AND r.tenant_id IS NOT NULL) THEN
    RAISE EXCEPTION 'rule_id names a platform rule or one of this tenant' USING ERRCODE = 'GRD01';
  END IF;
  IF TG_OP = 'INSERT' THEN
    RETURN NEW;
  END IF;

  IF OLD.status <> 'open' THEN
    RAISE EXCEPTION 'a closed issue is immutable' USING ERRCODE = 'GRD01';
  END IF;
  IF (NEW.tenant_id, NEW.listing_id, NEW.marketplace, NEW.listing_item_id, NEW.listing_version_id,
      NEW.validation_run_id, NEW.job_id, NEW.source, NEW.field_path, NEW.error_code,
      NEW.rule_id, NEW.error_map_id, NEW.dedupe_key, NEW.first_seen_at, NEW.recurrence_of_id,
      NEW.created_at)
     IS DISTINCT FROM
     (OLD.tenant_id, OLD.listing_id, OLD.marketplace, OLD.listing_item_id, OLD.listing_version_id,
      OLD.validation_run_id, OLD.job_id, OLD.source, OLD.field_path, OLD.error_code,
      OLD.rule_id, OLD.error_map_id, OLD.dedupe_key, OLD.first_seen_at, OLD.recurrence_of_id,
      OLD.created_at) THEN
    RAISE EXCEPTION 'an issue''s identity columns never change' USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.status NOT IN ('open', 'auto_fixed', 'resolved', 'dismissed') THEN
    RETURN NEW;  -- the status CHECK refuses
  END IF;
  IF NEW.status = 'dismissed' THEN
    IF NEW.severity = 'error' OR OLD.severity = 'error' THEN
      RAISE EXCEPTION 'an error is never dismissed' USING ERRCODE = 'GRD01';
    END IF;
    SELECT tm.role, tm.is_active INTO m FROM public.tenant_memberships tm
     WHERE tm.tenant_id = NEW.tenant_id AND tm.user_id = NEW.resolved_by_user_id FOR SHARE;
    IF NOT FOUND OR NOT (m.is_active AND m.role IN ('reviewer', 'owner')) THEN
      RAISE EXCEPTION 'a dismissal needs an active reviewer or owner' USING ERRCODE = 'GRD01';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER listing_issues_guard BEFORE INSERT OR UPDATE OR DELETE ON public.listing_issues
  FOR EACH ROW EXECUTE FUNCTION public.listing_issues_guard();

CREATE FUNCTION public.human_tasks_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status IN ('applied', 'cancelled') AND NOT coalesce(
         session_user = 'migrator'
         AND (nullif(current_setting('app.prune_table', true), '')::regclass = TG_RELID
              OR nullif(current_setting('app.purge_tenant', true), '')::uuid = OLD.tenant_id),
         false) THEN  -- an unset GUC compares NULL: refuse, never allow
      RAISE EXCEPTION 'a terminal task is deleted only by the prune or the purge'
        USING ERRCODE = 'GRD01';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status IN ('applied', 'cancelled') THEN
    RAISE EXCEPTION 'a terminal task is immutable' USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND (OLD.status, NEW.status) NOT IN (
       ('open', 'answered'), ('open', 'cancelled'), ('answered', 'applied'), ('answered', 'cancelled'))
  THEN
    RAISE EXCEPTION 'task status % -> % refused', OLD.status, NEW.status USING ERRCODE = 'GRD01';
  END IF;
  IF (NEW.kind, NEW.product_id, NEW.colourway_id, NEW.target_path, NEW.marketplace_account_id,
      NEW.image_id, NEW.question, NEW.reason, NEW.expected_answer, NEW.tenant_id, NEW.created_at)
     IS DISTINCT FROM
     (OLD.kind, OLD.product_id, OLD.colourway_id, OLD.target_path, OLD.marketplace_account_id,
      OLD.image_id, OLD.question, OLD.reason, OLD.expected_answer, OLD.tenant_id, OLD.created_at) THEN
    RAISE EXCEPTION 'a task''s kind, targets and question never change' USING ERRCODE = 'GRD01';
  END IF;
  IF OLD.answer IS NOT NULL AND (NEW.answer, NEW.answered_by_user_id, NEW.answered_at)
       IS DISTINCT FROM (OLD.answer, OLD.answered_by_user_id, OLD.answered_at) THEN
    RAISE EXCEPTION 'a task''s answer is set once' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER human_tasks_guard BEFORE UPDATE OR DELETE ON public.human_tasks
  FOR EACH ROW EXECUTE FUNCTION public.human_tasks_guard();

REVOKE DELETE, TRUNCATE ON public.listing_issues, public.human_tasks FROM app_rw;
