-- 0027/05_import_column_maps.down.sql (local and CI only)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.import_column_maps) THEN
    RAISE EXCEPTION '0027 downgrade refused: import_column_maps has rows' USING ERRCODE = 'GRD01';
  END IF;
END
$$;
DROP TABLE public.import_column_maps;
