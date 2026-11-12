-- 0038/03_held_repairs.down.sql (local and CI only): 0022's guard body, then drop the column
-- (its grants go with it).
CREATE OR REPLACE FUNCTION public.listing_repairs_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF OLD.retired_at IS NOT NULL THEN
    RAISE EXCEPTION 'a listing repair retires once' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;
ALTER TABLE public.listing_repairs DROP COLUMN held;
