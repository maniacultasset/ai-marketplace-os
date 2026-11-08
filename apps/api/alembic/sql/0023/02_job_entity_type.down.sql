-- 0023/02_job_entity_type.down.sql
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.job_runs WHERE entity_type = 'size_chart_read') THEN
    RAISE EXCEPTION '0023 downgrade refused: size_chart_read jobs exist';
  END IF;
END
$$;
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN ('listing', 'import', 'image', 'brand_document', 'image_candidate'));
