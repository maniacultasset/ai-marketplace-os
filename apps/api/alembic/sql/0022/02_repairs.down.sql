-- 0022/02_repairs.down.sql (local and CI only, RULES.md "No prod downgrade")
-- Fails loud while a repair exists: dropping the table would lose what was sent.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.listing_repairs) THEN
    RAISE EXCEPTION 'downgrade refused: listing_repairs has rows' USING ERRCODE = 'GRD01';
  END IF;
END
$$;

-- the 0018 bodies
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
    ELSE public.audit_unknown_entity(p_entity_type)
  END;
END
$$;

CREATE OR REPLACE FUNCTION public.purge_tenant_order() RETURNS text[]
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN ARRAY[
    'image_candidates', 'publish_field_results', 'listing_submissions', 'listing_issues',
    'human_tasks',
    'listing_items', 'review_decisions', 'validation_runs', 'agent_runs', 'performance',
    'import_rows', 'imports', 'idempotency_keys', 'job_runs', 'batches', 'listing_versions',
    'listings', 'oauth_states', 'refresh_tokens', 'variant_matrix_cells', 'images',
    'product_colourways', 'brand_chunks', 'brand_documents', 'marketplace_accounts', 'products',
    'brand_profiles', 'business_parties', 'password_reset_links', 'invitations', 'rules'];
END;

DROP TABLE public.listing_repairs;
DROP FUNCTION public.listing_repairs_guard();
