-- 0004/18_purge_tenant.up.sql
-- DATABASE.md §9 "Tenant lifecycle and DPDP erasure" (:1871-1874), §8.1 (migrator-only, SECURITY
-- INVOKER, EXECUTE granted to nobody). The purge sets app.purge_tenant for its transaction, which
-- is what the append-only and platform guards let the migrator delete under.
-- purge_tenant_order(): every table with tenant_id except tenants, tenant_memberships and
-- audit_log, children first, in an order that respects every FK (with the deferrable version
-- pointers deferred): the submission ledger, then issues (they name items) and tasks, before
-- items and jobs;
-- decisions and jobs before batches; imports before jobs; versions before listings; cells and
-- images before colourways; products before parties; brand_profiles before business_parties.
-- purge_tenant(t, 'data'): those tables, then actor_email and ip nulled in the tenant's audit
--   rows, users with no other membership pseudonymised, data_purged_at stamped.
-- purge_tenant(t, 'final') (after 'data'): the tenant's audit rows, then the tenant (its
--   memberships cascade), then the users left with no membership.
-- Each phase writes a `tenant.purged` platform tombstone (NULL tenant) with {mode, row_counts,
-- backups_expire_by}. The storage prefix is deleted by the calling job, not here.
CREATE FUNCTION public.purge_tenant_order() RETURNS text[]
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

CREATE FUNCTION public.purge_tenant(t uuid, mode text) RETURNS jsonb
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  tbl text;
  n bigint;
  counts jsonb := '{}'::jsonb;
  members uuid[];
BEGIN
  IF mode NOT IN ('data', 'final') THEN
    RAISE EXCEPTION 'purge_tenant mode is data or final, not %', mode USING ERRCODE = 'GRD01';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.tenants x WHERE x.id = t) THEN
    RAISE EXCEPTION 'no tenant %', t USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.purge_tenant', t::text, true);
  SET CONSTRAINTS ALL DEFERRED;

  IF mode = 'data' THEN
    FOREACH tbl IN ARRAY public.purge_tenant_order() LOOP
      EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1', tbl) USING t;
      GET DIAGNOSTICS n = ROW_COUNT;
      counts := counts || jsonb_build_object(tbl, n);
    END LOOP;
    UPDATE public.audit_log SET actor_email = NULL, ip = NULL
     WHERE tenant_id = t AND (actor_email IS NOT NULL OR ip IS NOT NULL);
    UPDATE public.users u SET email = 'erased+' || u.id || '@invalid', hashed_password = NULL,
                              display_name = NULL, erased_at = coalesce(u.erased_at, now())
     WHERE u.id IN (SELECT m.user_id FROM public.tenant_memberships m WHERE m.tenant_id = t)
       AND NOT EXISTS (SELECT 1 FROM public.tenant_memberships o
                        WHERE o.user_id = u.id AND o.tenant_id <> t);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('users_pseudonymised', n);
    UPDATE public.tenants SET data_purged_at = now() WHERE id = t;
  ELSE
    IF (SELECT x.data_purged_at FROM public.tenants x WHERE x.id = t) IS NULL THEN
      RAISE EXCEPTION 'purge_tenant final runs after the data phase' USING ERRCODE = 'GRD01';
    END IF;
    SELECT array_agg(m.user_id) INTO members FROM public.tenant_memberships m WHERE m.tenant_id = t;
    DELETE FROM public.audit_log WHERE tenant_id = t;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('audit_log', n);
    DELETE FROM public.tenants WHERE id = t;
    counts := counts || jsonb_build_object('tenant_memberships', coalesce(cardinality(members), 0));
    DELETE FROM public.users u
     WHERE u.id = ANY (coalesce(members, ARRAY[]::uuid[]))
       AND NOT EXISTS (SELECT 1 FROM public.tenant_memberships o WHERE o.user_id = u.id);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('users', n);
  END IF;

  INSERT INTO public.audit_log (tenant_id, actor_type, action, entity_type, entity_id, after)
  VALUES (NULL, 'system', 'tenant.purged', 'tenant', t,
          jsonb_build_object('mode', mode, 'row_counts', counts,
                             'backups_expire_by', now() + interval '35 days'));
  RETURN counts;
END
$$;
