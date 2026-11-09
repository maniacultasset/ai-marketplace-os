-- 0027/03_task_kind.down.sql (local and CI only)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.human_tasks WHERE kind = 'category_review') THEN
    RAISE EXCEPTION '0027 downgrade refused: category_review tasks exist' USING ERRCODE = 'GRD01';
  END IF;
END
$$;
DROP INDEX public.human_tasks_one_category_review;
ALTER TABLE public.human_tasks DROP CONSTRAINT ck_human_tasks_category_review;
-- the 0002 list
ALTER TABLE public.human_tasks DROP CONSTRAINT ck_human_tasks_kind;
ALTER TABLE public.human_tasks ADD CONSTRAINT ck_human_tasks_kind CHECK (kind IN (
    'missing_fact', 'conflicting_facts', 'unverified_declaration', 'approval_or_evidence',
    'permission_decision', 'reconnect_account', 'marketplace_error_review', 'image_replacement'));
