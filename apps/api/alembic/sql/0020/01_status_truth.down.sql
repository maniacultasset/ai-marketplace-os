-- 0020/01_status_truth.down.sql (local and CI only, RULES.md "No prod downgrade")
-- Fails loud while a (published, inactive) listing exists: the old CHECK cannot hold it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.listings
              WHERE status = 'published' AND marketplace_state = 'inactive') THEN
    RAISE EXCEPTION 'downgrade refused: a published listing is at marketplace_state inactive'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;

ALTER TABLE public.products
  DROP CONSTRAINT ck_products_photo_rejections,
  DROP COLUMN photo_rejections;
DROP FUNCTION public.photo_rejections_valid(jsonb);

CREATE OR REPLACE VIEW public.listing_status_display WITH (security_invoker = true) AS
SELECT l.tenant_id, l.id AS listing_id, l.product_id, l.marketplace, l.status, l.status_reason,
       l.marketplace_state,
       public.listing_display_status(l.status, l.status_reason, l.marketplace_state,
                                     l.paused_at IS NOT NULL, l.archived_at IS NOT NULL)
         AS display_status,
       public.listing_display_step(l.status, l.marketplace_state) AS display_step,
       l.marketplace_state IN ('live', 'partially_live') AS is_live,
       (l.status = 'blocked'
        AND coalesce(l.status_reason IN ('human_task_open', 'marketplace_action_required'), false))
         AS human_action_required,
       (l.live_version_id IS NOT NULL AND l.live_version_id IS DISTINCT FROM l.current_version_id)
         AS live_with_draft,
       l.external_id, l.marketplace_sku, l.live_url, l.last_synced_at, l.last_event,
       l.last_event_at
  FROM public.listings l;

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

ALTER TABLE public.listings
  DROP CONSTRAINT ck_published,
  ADD CONSTRAINT ck_published CHECK (
    status <> 'published'
    OR (published_version_id IS NOT NULL AND first_published_at IS NOT NULL
        AND marketplace_state IN ('processing', 'pending_review', 'live', 'partially_live'))
  ) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_published;
