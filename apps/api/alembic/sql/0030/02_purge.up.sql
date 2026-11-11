-- 0030/02_purge.up.sql: purge_tenant_order() is the head's body (0027/07_purge; 0028 and 0029
-- leave it alone) plus sku_counters (no row references it, so it goes with the products).
CREATE OR REPLACE FUNCTION public.purge_tenant_order() RETURNS text[]
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN ARRAY[
    'image_candidates', 'listing_repairs', 'publish_field_results', 'listing_submissions', 'listing_issues',
    'human_tasks',
    'listing_items', 'review_decisions', 'validation_runs', 'agent_runs', 'performance',
    'import_rows', 'imports', 'import_column_maps', 'idempotency_keys', 'job_runs', 'batches',
    'listing_versions',
    'listings', 'oauth_states', 'refresh_tokens', 'variant_matrix_cells', 'images',
    'product_colourways', 'brand_chunks', 'brand_documents', 'marketplace_accounts', 'products',
    'sku_counters',
    'brand_profiles', 'business_parties', 'password_reset_links', 'invitations', 'rules'];
END;
