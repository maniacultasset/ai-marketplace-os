-- 0004/07_review_backstop.up.sql
-- DATABASE.md §5.3 review_decisions "Bulk backstop" (:804; T42): an `approve` row whose batch has
-- more than one item needs the listing `validated` → REV01 (→ 409
-- BULK_APPROVE_REQUIRES_VALIDATED). A batch of one, or no batch, is a single approval.
CREATE FUNCTION public.review_bulk_backstop() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF NEW.decision = 'approve' AND NEW.batch_id IS NOT NULL
     AND (SELECT b.item_count FROM public.batches b
           WHERE b.tenant_id = NEW.tenant_id AND b.id = NEW.batch_id) > 1
     AND NOT EXISTS (SELECT 1 FROM public.listings l
                      WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.listing_id
                        AND l.status = 'validated') THEN
    RAISE EXCEPTION 'a bulk approval needs a validated listing' USING ERRCODE = 'REV01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER review_bulk_backstop BEFORE INSERT ON public.review_decisions
  FOR EACH ROW EXECUTE FUNCTION public.review_bulk_backstop();
