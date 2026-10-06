-- 0004/02_audit.up.sql
-- DATABASE.md §5.2 audit_log "What before/after may hold" (rev 3 allow-list) and "Inserts", §4
-- "Helper grants". `audit_allowlist(entity_type)` is the per-entity allow-list of non-personal
-- top-level keys; an unknown entity type raises (every audited entity must have an entry).
-- Fact writes keep the changed keys' Fact envelopes under `attributes` and `provenance` (where
-- products and brand_profiles store them, §5.6). `audit()` filters `before`/`after` to the list
-- and INSERTs without RETURNING (RETURNING would also have to pass the SELECT policy).
CREATE FUNCTION public.audit_allowlist(p_entity_type text) RETURNS text[]
  LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  RETURN CASE p_entity_type
    WHEN 'product' THEN ARRAY['attributes', 'provenance']
    WHEN 'brand_profile' THEN ARRAY['provenance']
    WHEN 'listing' THEN ARRAY['status', 'status_reason', 'paused_at', 'archived_at']
    WHEN 'rules' THEN ARRAY['enabled']
    WHEN 'membership' THEN ARRAY['role', 'is_active']
    WHEN 'tenant' THEN ARRAY['name', 'tone', 'guidelines', 'glossary']
    WHEN 'marketplace_account' THEN ARRAY['status', 'settings']
    WHEN 'business_party' THEN ARRAY['roles', 'source']
    WHEN 'category_template' THEN ARRAY['marketplace', 'category']
    WHEN 'job_run' THEN ARRAY['kind', 'status', 'parent_job_id']
    WHEN 'image' THEN ARRAY['slot', 'status', 'deleted_at']
    WHEN 'listing_issue' THEN ARRAY['status', 'resolution']
    WHEN 'marketplace_error_map' THEN ARRAY['enabled']
    WHEN 'brand_document' THEN ARRAY[]::text[]
    WHEN 'user' THEN ARRAY[]::text[]
    ELSE public.audit_unknown_entity(p_entity_type)
  END;
END
$$;

-- Split out so the CASE above stays one expression; raises for any entity without an entry.
CREATE FUNCTION public.audit_unknown_entity(p_entity_type text) RETURNS text[]
  LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  RAISE EXCEPTION 'audit: no allow-list entry for entity type %', p_entity_type
    USING ERRCODE = 'GRD01';
END
$$;

CREATE FUNCTION public.audit(
  p_tenant_id uuid, p_actor_type text, p_actor_user_id uuid, p_action text, p_entity_type text,
  p_entity_id uuid, p_before jsonb, p_after jsonb, p_reason text, p_request_id text,
  p_job_id uuid, p_ip inet, p_actor_email text
) RETURNS void
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  keep text[] := public.audit_allowlist(p_entity_type);
BEGIN
  INSERT INTO public.audit_log (
    tenant_id, actor_type, actor_user_id, actor_email, action, entity_type, entity_id,
    before, after, request_id, reason, job_id, ip)
  VALUES (
    p_tenant_id, p_actor_type, p_actor_user_id, p_actor_email, p_action, p_entity_type,
    p_entity_id,
    CASE WHEN p_before IS NOT NULL THEN coalesce(
      (SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(p_before) AS e
       WHERE e.key = ANY (keep)), '{}'::jsonb) END,
    CASE WHEN p_after IS NOT NULL THEN coalesce(
      (SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(p_after) AS e
       WHERE e.key = ANY (keep)), '{}'::jsonb) END,
    p_request_id, p_reason, p_job_id, p_ip);
END
$$;

-- audit() is SECURITY INVOKER, so its caller also needs the allow-list functions.
GRANT EXECUTE ON FUNCTION public.audit_allowlist(text), public.audit_unknown_entity(text),
  public.audit TO app_rw;
