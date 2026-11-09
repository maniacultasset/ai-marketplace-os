-- 0027/04_job_entity_type.up.sql (SI14, D5): a classification and a column-map job are
-- `image_compliance` jobs about a `category_suggestion` and a `column_map` (DB-only entity types;
-- JobKind stays closed, as 0023/02). The list is the head's (0024_brief_gaps) plus the two.
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal', 'category_suggestion', 'column_map'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;
