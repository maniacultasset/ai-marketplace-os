-- 0020/01_status_truth.up.sql
-- SI9 (errors and status truth). Expand only, in one transaction:
-- 1. ck_published also allows marketplace_state = inactive: the product is on the marketplace
--    (an ASIN exists) but not buyable yet, so the listing stays published and keeps syncing.
-- 2. listing_display_status shows (published, inactive) as human_action_required (the seller adds
--    an offer or stock), and listing_status_display.human_action_required says the same.
-- 3. products.photo_rejections: {V1 photo key: the rejected value}, so a value the seller rejected
--    is never written back by the next photo read.
-- squawk constraint-missing-not-valid (NOT VALID and VALIDATE in one transaction): accepted as in
-- 0014/0015. listings and products are small in v1, and one transaction keeps the CHECK swap atomic.
ALTER TABLE public.listings
  DROP CONSTRAINT ck_published,
  ADD CONSTRAINT ck_published CHECK (
    status <> 'published'
    OR (published_version_id IS NOT NULL AND first_published_at IS NOT NULL
        AND marketplace_state IN ('processing', 'pending_review', 'live', 'partially_live',
                                  'inactive'))
  ) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_published;

CREATE OR REPLACE FUNCTION public.listing_display_status(
  p_status text, p_status_reason text, p_marketplace_state text, p_paused boolean,
  p_archived boolean
) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN CASE
    WHEN p_archived THEN 'archived'
    WHEN p_paused THEN 'paused'
    WHEN p_status = 'blocked'
         AND p_status_reason IN ('human_task_open', 'marketplace_action_required')
      THEN 'human_action_required'
    WHEN p_status = 'published' AND p_marketplace_state = 'inactive'
      THEN 'human_action_required'
    WHEN p_marketplace_state IN ('live', 'partially_live') THEN p_marketplace_state
    ELSE CASE p_status
      WHEN 'created' THEN 'draft'
      WHEN 'generating' THEN 'understanding'
      WHEN 'validated' THEN 'ready_for_review'
      WHEN 'needs_review' THEN 'ready_for_review'
      WHEN 'approved' THEN 'ready_to_list'
      WHEN 'publishing' THEN 'submitting'
      WHEN 'published' THEN 'processing'
      WHEN 'blocked' THEN 'blocked'
      WHEN 'failed' THEN 'error'
    END
  END;
END;

CREATE OR REPLACE VIEW public.listing_status_display WITH (security_invoker = true) AS
SELECT l.tenant_id, l.id AS listing_id, l.product_id, l.marketplace, l.status, l.status_reason,
       l.marketplace_state,
       public.listing_display_status(l.status, l.status_reason, l.marketplace_state,
                                     l.paused_at IS NOT NULL, l.archived_at IS NOT NULL)
         AS display_status,
       public.listing_display_step(l.status, l.marketplace_state) AS display_step,
       l.marketplace_state IN ('live', 'partially_live') AS is_live,
       ((l.status = 'blocked'
         AND coalesce(l.status_reason IN ('human_task_open', 'marketplace_action_required'), false))
        OR (l.status = 'published' AND l.marketplace_state = 'inactive'))
         AS human_action_required,
       (l.live_version_id IS NOT NULL AND l.live_version_id IS DISTINCT FROM l.current_version_id)
         AS live_with_draft,
       l.external_id, l.marketplace_sku, l.live_url, l.last_synced_at, l.last_event,
       l.last_event_at
  FROM public.listings l;

-- An object of at most the 9 SI3 photo keys (services/photo_facts.py V1_KEYS), each a string of
-- 1-40 characters. Returns false on any input and never raises.
CREATE FUNCTION public.photo_rejections_valid(p jsonb) RETURNS boolean
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN CASE
    WHEN p IS NULL THEN false
    WHEN pg_catalog.jsonb_typeof(p) <> 'object' THEN false
    ELSE coalesce((
      SELECT pg_catalog.bool_and(CASE
        WHEN e.k <> ALL (ARRAY['neckline', 'sleeve_length', 'fit', 'garment_length', 'embroidery',
          'occasion', 'color', 'pattern', 'number_of_pieces']) THEN false
        WHEN pg_catalog.jsonb_typeof(e.v) <> 'string' THEN false
        WHEN pg_catalog.char_length(e.v #>> '{}') NOT BETWEEN 1 AND 40 THEN false
        ELSE true
      END)
      FROM pg_catalog.jsonb_each(p) AS e(k, v)
    ), true)
  END;
END;

GRANT EXECUTE ON FUNCTION public.photo_rejections_valid(jsonb) TO app_rw, app_platform_ro, app_ro;

-- products is table-granted (0001 GRANTS_UP), so the new column needs no column grant.
ALTER TABLE public.products
  ADD COLUMN photo_rejections jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD CONSTRAINT ck_products_photo_rejections
    CHECK (public.photo_rejections_valid(photo_rejections)) NOT VALID;
ALTER TABLE public.products VALIDATE CONSTRAINT ck_products_photo_rejections;
