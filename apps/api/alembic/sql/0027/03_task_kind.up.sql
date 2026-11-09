-- 0027/03_task_kind.up.sql (SI14, D4): `category_review` asks the seller to pick a category when
-- the classifier's top confidence is below 0.80. It names a product, never a fact path or an
-- account. One live review per product (index inference in the worker's ON CONFLICT).
ALTER TABLE public.human_tasks DROP CONSTRAINT ck_human_tasks_kind;
ALTER TABLE public.human_tasks ADD CONSTRAINT ck_human_tasks_kind CHECK (kind IN (
    'missing_fact', 'conflicting_facts', 'unverified_declaration', 'approval_or_evidence',
    'permission_decision', 'reconnect_account', 'marketplace_error_review', 'image_replacement',
    'category_review'))
  NOT VALID;
ALTER TABLE public.human_tasks VALIDATE CONSTRAINT ck_human_tasks_kind;

ALTER TABLE public.human_tasks ADD CONSTRAINT ck_human_tasks_category_review CHECK (
    kind <> 'category_review'
    OR (product_id IS NOT NULL AND target_path IS NULL AND marketplace_account_id IS NULL))
  NOT VALID;
ALTER TABLE public.human_tasks VALIDATE CONSTRAINT ck_human_tasks_category_review;

-- Built in the transaction, not CONCURRENTLY: no row can match yet (the kind is new) and the
-- table is small in v1; the SHARE lock is bounded by lock_timeout.
CREATE UNIQUE INDEX human_tasks_one_category_review
  ON public.human_tasks (tenant_id, product_id)
  WHERE kind = 'category_review' AND status IN ('open', 'answered');
