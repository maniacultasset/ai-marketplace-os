-- 0035/01_job_entity_type.up.sql (V1.1, client feedback item 9: does a photo show this product in
-- this colour): a photo match is an `image_compliance` job about a `photo_match` (entity = the
-- image; a DB-only entity type, JobKind stays closed, as 0023/02, 0027/04 and 0028/01). The list
-- is the head's (0034, unchanged from 0028) plus the one.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal', 'category_suggestion', 'column_map', 'photo_facts_read', 'photo_match'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
