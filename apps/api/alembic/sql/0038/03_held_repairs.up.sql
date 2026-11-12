-- 0038/03_held_repairs.up.sql (client item 20): a repair Kuinn found while the brand's
-- `auto_fix_enabled` is off is stored held, a suggested fix. The adapter never applies a held
-- repair; the seller's "Apply fixes" releases it once (held true -> false), and it then behaves as
-- any live repair. A repair is never held again. Existing rows stay live (DEFAULT false: a
-- catalog-only add on PostgreSQL 17, no rewrite).
ALTER TABLE public.listing_repairs ADD COLUMN held boolean NOT NULL DEFAULT false;

-- 0022's guard plus the one held change: a retired repair never changes, and `held` only goes
-- from true to false (on a live row).
CREATE OR REPLACE FUNCTION public.listing_repairs_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF OLD.retired_at IS NOT NULL THEN
    RAISE EXCEPTION 'a listing repair retires once' USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.held AND NOT OLD.held THEN
    RAISE EXCEPTION 'a listing repair is never held again' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

GRANT UPDATE (held) ON public.listing_repairs TO app_rw;
GRANT SELECT (held) ON public.listing_repairs TO app_ro, app_platform_ro;
