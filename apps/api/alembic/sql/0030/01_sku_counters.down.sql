-- 0030/01_sku_counters.down.sql (local and CI only)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.sku_counters) THEN
    RAISE EXCEPTION '0030 downgrade refused: SKU counters exist'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
DROP TABLE public.sku_counters;
