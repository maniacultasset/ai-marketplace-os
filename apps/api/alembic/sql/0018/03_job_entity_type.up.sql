-- 0018/03_job_entity_type.up.sql
-- SI7: a candidate job is an `image_compliance` job about an `image_candidate` (DB-only entity
-- type; JobKind stays closed). NOT VALID then VALIDATE: the validate scans job_runs under SHARE
-- UPDATE EXCLUSIVE; only the drop and add take ACCESS EXCLUSIVE, briefly.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN ('listing', 'import', 'image', 'brand_document', 'image_candidate'))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
