-- 0014/02_audit_allowlist.up.sql
-- DATABASE.md §5.2 audit_log allow-list: 0004's body plus the DW2 entity
-- product_type_definition (non-personal keys only; the schema link is never one of them).
CREATE OR REPLACE FUNCTION public.audit_allowlist(p_entity_type text) RETURNS text[]
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
    WHEN 'product_type_definition' THEN ARRAY['marketplace_id', 'product_type', 'requirements',
                                              'locale', 'product_type_version']
    ELSE public.audit_unknown_entity(p_entity_type)
  END;
END
$$;
