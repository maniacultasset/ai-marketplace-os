-- 0018/03_job_entity_type.down.sql
DELETE FROM public.job_runs WHERE entity_type = 'image_candidate';
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN ('listing', 'import', 'image', 'brand_document'));
