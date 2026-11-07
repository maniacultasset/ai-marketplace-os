-- 0017/04_allowlist_erasure.down.sql
-- Restores the parents' bodies verbatim: 0014's audit_allowlist, 0004's purge_tenant_order and
-- 0013's redact_user. Audit rows of entity `invitation` stay (append-only); the allow-list is read
-- only at write time.
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

CREATE OR REPLACE FUNCTION public.purge_tenant_order() RETURNS text[]
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN ARRAY[
    'publish_field_results', 'listing_submissions', 'listing_issues', 'human_tasks',
    'listing_items', 'review_decisions', 'validation_runs', 'agent_runs', 'performance',
    'import_rows', 'imports', 'idempotency_keys', 'job_runs', 'batches', 'listing_versions',
    'listings', 'oauth_states', 'refresh_tokens', 'variant_matrix_cells', 'images',
    'product_colourways', 'brand_chunks', 'brand_documents', 'marketplace_accounts', 'products',
    'brand_profiles', 'business_parties', 'invitations', 'rules'];
END;

CREATE OR REPLACE FUNCTION public.redact_user(u uuid) RETURNS void
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  old_email text;
BEGIN
  SELECT x.email INTO old_email FROM public.users x WHERE x.id = u FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no user %', u USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.redact_user', u::text, true);
  DELETE FROM public.login_flows WHERE lower(email) = lower(old_email);
  UPDATE public.users SET email = 'erased+' || u || '@invalid', hashed_password = NULL,
                          display_name = NULL, erased_at = coalesce(erased_at, now()),
                          failed_auth_count = 0, locked_until = NULL,
                          password_must_change = false, google_sub = NULL
   WHERE id = u;
  DELETE FROM public.user_mfa_factors WHERE user_id = u;
  DELETE FROM public.user_recovery_codes WHERE user_id = u;
  DELETE FROM public.mfa_challenges WHERE user_id = u;
  DELETE FROM public.user_password_history WHERE user_id = u;
  DELETE FROM public.trusted_devices WHERE user_id = u;
  UPDATE public.tenant_memberships SET is_active = false, deactivated_at = coalesce(deactivated_at, now())
   WHERE user_id = u AND is_active;
  DELETE FROM public.refresh_tokens WHERE user_id = u;
  DELETE FROM public.invitations
   WHERE accepted_user_id = u OR lower(email) = lower(old_email);
  UPDATE public.audit_log SET actor_email = NULL, ip = NULL
   WHERE actor_user_id = u AND (actor_email IS NOT NULL OR ip IS NOT NULL);
END
$$;
