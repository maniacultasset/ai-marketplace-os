-- 0018/04_allowlist_purge.up.sql
-- SI7: audit_allowlist is 0017's body plus the image_candidate entity (kind and status only);
-- purge_tenant_order() is 0017's array with image_candidates first, ahead of agent_runs and
-- images, which it references.
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
