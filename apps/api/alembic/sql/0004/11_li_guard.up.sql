-- 0004/11_li_guard.up.sql
-- DATABASE.md §5.3 listing_items "li_guard" (:847-848). On INSERT, and when the role, product or
-- listing changes, the item's product is read FOR SHARE (a concurrent soft-delete waits):
--   standalone = the listing's product, with no live children;
--   parent     = the listing's product, with a variation theme;
--   child      = a live product whose parent_product_id is the listing's product.
-- On INSERT seller_sku = products.sku. Once first_submitted_at is set, seller_sku, product_id,
-- item_role and listing_id are immutable. `live` is written only by a publish or status-sync job
-- (app.job_id names one). Violations → LIT01 (→ 409 SELLER_SKU_CONFLICT / INVALID_ITEM).
-- app_rw may not DELETE items.
CREATE FUNCTION public.li_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  p record;
  listing_product uuid;
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.first_submitted_at IS NOT NULL AND (
       NEW.seller_sku IS DISTINCT FROM OLD.seller_sku OR NEW.product_id IS DISTINCT FROM OLD.product_id
       OR NEW.item_role IS DISTINCT FROM OLD.item_role OR NEW.listing_id IS DISTINCT FROM OLD.listing_id)
  THEN
    RAISE EXCEPTION 'a submitted item keeps its sku, product, role and listing' USING ERRCODE = 'LIT01';
  END IF;

  IF TG_OP = 'INSERT' OR NEW.product_id IS DISTINCT FROM OLD.product_id
     OR NEW.item_role IS DISTINCT FROM OLD.item_role OR NEW.listing_id IS DISTINCT FROM OLD.listing_id
  THEN
    SELECT l.product_id INTO listing_product FROM public.listings l
     WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.listing_id;
    SELECT x.sku, x.parent_product_id, x.variation_theme, x.deleted_at INTO p
      FROM public.products x WHERE x.tenant_id = NEW.tenant_id AND x.id = NEW.product_id FOR SHARE;
    IF NOT FOUND OR listing_product IS NULL THEN
      RETURN NEW;  -- the composite FKs refuse
    END IF;
    IF NEW.item_role = 'standalone' AND (
         NEW.product_id <> listing_product
         OR EXISTS (SELECT 1 FROM public.products c
                     WHERE c.tenant_id = NEW.tenant_id AND c.parent_product_id = NEW.product_id
                       AND c.deleted_at IS NULL)) THEN
      RAISE EXCEPTION 'a standalone item is the listing''s product, with no live children'
        USING ERRCODE = 'LIT01';
    END IF;
    IF NEW.item_role = 'parent' AND (NEW.product_id <> listing_product OR p.variation_theme IS NULL)
    THEN
      RAISE EXCEPTION 'a parent item is the listing''s product, with a theme' USING ERRCODE = 'LIT01';
    END IF;
    IF NEW.item_role = 'child' AND (p.deleted_at IS NOT NULL
                                    OR p.parent_product_id IS DISTINCT FROM listing_product) THEN
      RAISE EXCEPTION 'a child item is a live variant of the listing''s product'
        USING ERRCODE = 'LIT01';
    END IF;
    IF TG_OP = 'INSERT' AND NEW.seller_sku IS DISTINCT FROM p.sku THEN
      RAISE EXCEPTION 'seller_sku must equal the product''s sku' USING ERRCODE = 'LIT01';
    END IF;
  END IF;

  IF NEW.marketplace_state = 'live'
     AND (TG_OP = 'INSERT' OR OLD.marketplace_state IS DISTINCT FROM 'live')
     AND NOT EXISTS (SELECT 1 FROM public.job_runs j
                      WHERE j.id = public.app_job_id()
                        AND j.kind IN ('listing_publish', 'listing_status_sync')) THEN
    RAISE EXCEPTION 'live is written only by a publish or status-sync job' USING ERRCODE = 'LIT01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER li_guard BEFORE INSERT OR UPDATE ON public.listing_items
  FOR EACH ROW EXECUTE FUNCTION public.li_guard();

REVOKE DELETE, TRUNCATE ON public.listing_items FROM app_rw;
