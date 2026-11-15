-- 0040/03_purge_order.up.sql: purge_tenant_order() is the head's body (0034/02_purge; 0035 to
-- 0039 leave it alone) with photo_links first, ahead of the images, products and colourways it
-- references.
CREATE OR REPLACE FUNCTION public.purge_tenant_order() RETURNS text[]
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN ARRAY[
    'photo_links',
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
