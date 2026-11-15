-- 0040/02_job_entity_type.up.sql (G2): a sheet photo download is an `image_compliance` job about
-- a `photo_links` entity (entity = the target product; a DB-only entity type, JobKind stays
-- closed, as 0035/01). The list is the head's (0035, unchanged by 0036 to 0039) plus the one.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal', 'category_suggestion', 'column_map', 'photo_facts_read', 'photo_match',
    'photo_links'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
