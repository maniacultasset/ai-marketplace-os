-- 0030/01_brand_voice.down.sql (local and CI only)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.brand_profiles WHERE brand_voice IS NOT NULL) THEN
    RAISE EXCEPTION '0030 downgrade refused: a brand voice is stored'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
ALTER TABLE public.brand_profiles DROP CONSTRAINT ck_brand_profiles_brand_voice;
ALTER TABLE public.brand_profiles DROP COLUMN brand_voice;
