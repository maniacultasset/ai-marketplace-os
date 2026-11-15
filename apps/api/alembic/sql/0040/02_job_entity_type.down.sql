-- 0040/02_job_entity_type.down.sql (local and CI only): the parent's (0039, unchanged from 0035)
-- list
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.job_runs WHERE entity_type = 'photo_links') THEN
    RAISE EXCEPTION '0040 downgrade refused: photo_links jobs exist'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal', 'category_suggestion', 'column_map', 'photo_facts_read', 'photo_match'
  ));
