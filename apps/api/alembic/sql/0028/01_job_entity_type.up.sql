-- 0028/01_job_entity_type.up.sql (V1.1, read the photos on upload): a photo read on upload is an
-- `image_compliance` job about a `photo_facts_read` (entity = the product; a DB-only entity type,
-- JobKind stays closed, as 0023/02 and 0027/04). The list is the head's (0027) plus the one.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal', 'category_suggestion', 'column_map', 'photo_facts_read'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
