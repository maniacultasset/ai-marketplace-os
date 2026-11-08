-- 0023/02_job_entity_type.up.sql
-- SI12: a size-chart read is an `image_compliance` job about a `size_chart_read` (DB-only entity
-- type; JobKind stays closed), so it takes the brand's AI image lock, never the upload lock.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
