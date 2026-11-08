-- 0024/01_checks.up.sql (SI13): no transition edge (the recategorise move rides the existing
-- user `edit` edge failed -> blocked, 0004/01_transitions.up.sql:78).
-- 1. listing_repairs.rule takes `fill_variant_axis` (a child's missing theme attribute).
ALTER TABLE public.listing_repairs DROP CONSTRAINT ck_listing_repairs_rule;
ALTER TABLE public.listing_repairs ADD CONSTRAINT ck_listing_repairs_rule CHECK (rule IN (
    'enum_fold', 'unit_format', 'identifier_normalize', 'exemption_flag', 'fill_from_facts',
    'drop_locked', 'image_slot', 'fill_variant_axis'))
  NOT VALID;
ALTER TABLE public.listing_repairs VALIDATE CONSTRAINT ck_listing_repairs_rule;

-- 2. job_runs.entity_type takes `error_proposal`: a diagnosis is a `brand_embed` job about a
-- platform proposal (DB-only entity type; JobKind stays closed, as 0018/03 and 0023/02).
ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_entity_type;
ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_entity_type
  CHECK (entity_type IN (
    'listing', 'import', 'image', 'brand_document', 'image_candidate', 'size_chart_read',
    'error_proposal'
  ))
  NOT VALID;
ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_entity_type;

-- 3. audit_allowlist is 0022's body plus marketplace_error_proposal (status only).
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
    WHEN 'invitation' THEN ARRAY['role']
    WHEN 'image_candidate' THEN ARRAY['kind', 'status']
    WHEN 'listing_repair' THEN ARRAY['rule', 'attribute']
    WHEN 'marketplace_error_proposal' THEN ARRAY['status']
    ELSE public.audit_unknown_entity(p_entity_type)
  END;
END
$$;
