-- 0004/17_display.up.sql
-- DATABASE.md §6.3 "BRD display projection" (:1491-1502; T12, T13): nothing is stored. Two
-- IMMUTABLE SQL functions map the stored columns to the 14 BRD names (lowercase wire values), so
-- renaming a display value is a function replace. Precedence, first match:
--   archived / paused overlays; blocked for human_task_open or marketplace_action_required →
--   human_action_required; marketplace_state live / partially_live; else the step:
--   created draft · generating understanding (classifying reserved) · validated, needs_review
--   ready_for_review · approved ready_to_list · publishing submitting · published processing ·
--   blocked blocked · failed error.
-- listing_display_step is the step alone (the BRD "current step"), with a published listing's
-- live / partially_live marketplace state, and no overlay.
-- Two security_invoker views (RLS applies to the caller): listing_status_display and
-- product_status_display (the BRD workflow columns: overall_status, open_issue_count,
-- human_action_required, last_updated). app_rw reads them; nothing writes through them.
CREATE FUNCTION public.listing_display_step(p_status text, p_marketplace_state text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN CASE
    WHEN p_status = 'published' AND p_marketplace_state IN ('live', 'partially_live')
      THEN p_marketplace_state
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

CREATE FUNCTION public.listing_display_status(
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

GRANT EXECUTE ON FUNCTION public.listing_display_step(text, text),
  public.listing_display_status(text, text, text, boolean, boolean) TO app_rw, app_ro, app_platform_ro;

CREATE VIEW public.listing_status_display WITH (security_invoker = true) AS
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

CREATE VIEW public.product_status_display WITH (security_invoker = true) AS
WITH per_listing AS (
  SELECT d.tenant_id, d.product_id, d.display_status, d.human_action_required, d.last_event_at,
         d.last_synced_at,
         (SELECT count(*) FROM public.listing_issues i
           WHERE i.tenant_id = d.tenant_id AND i.listing_id = d.listing_id AND i.status = 'open')
           AS open_issues
    FROM public.listing_status_display d
)
SELECT p.tenant_id, p.id AS product_id,
       CASE
         WHEN bool_and(x.display_status = 'archived') THEN 'archived'
         WHEN bool_and(x.display_status IN ('live', 'archived')) THEN 'live'
         WHEN bool_or(x.display_status IN ('live', 'partially_live')) THEN 'partially_live'
         WHEN bool_or(x.display_status IN ('human_action_required', 'blocked')) THEN 'blocked'
         WHEN bool_or(x.display_status = 'error') THEN 'error'
         WHEN bool_or(x.display_status IN ('submitting', 'processing')) THEN 'processing'
         WHEN bool_and(x.display_status IN ('paused', 'archived')) THEN 'paused'
         ELSE (ARRAY['draft', 'understanding', 'classifying', 'ready_for_review', 'ready_to_list'])[
                max(array_position(
                  ARRAY['draft', 'understanding', 'classifying', 'ready_for_review', 'ready_to_list'],
                  x.display_status))]
       END AS overall_status,
       sum(x.open_issues)::integer AS open_issue_count,
       bool_or(x.human_action_required) AS human_action_required,
       GREATEST(p.updated_at, max(x.last_event_at)) AS last_activity_at,
       max(x.last_synced_at) AS last_synced_at
  FROM public.products p
  JOIN per_listing x ON x.tenant_id = p.tenant_id AND x.product_id = p.id
 GROUP BY p.tenant_id, p.id, p.updated_at;

-- the 0000 default privileges grant app_rw DML on new relations; these views are read-only
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.listing_status_display,
  public.product_status_display FROM app_rw;
