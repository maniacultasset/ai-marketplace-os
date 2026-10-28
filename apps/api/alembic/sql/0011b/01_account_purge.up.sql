-- 0011b/01_account_purge.up.sql
-- Amazon DPP 1.7 (delete within 30 days of the seller revoking or removing access) and the
-- Flipkart equivalent: purge_marketplace_account(t, a [, force]) deletes everything Kuinn holds
-- because of one marketplace account while its tenant stays:
--   * every listing still linked to the account (`listings.marketplace_account_id = a`) and its
--     whole tree: submissions and their per-field results, items, issues, review decisions,
--     validation and agent runs, performance rows, jobs and versions. A listing a reconnect of the
--     same seller moved to the new account (`_adopt_revoked`) took its submissions with it
--     (02_ls_guard_repoint), so its history is never touched here. A listing is the
--     marketplace-specific copy of a product: the product, its colourways, images and facts
--     stay, so the brand can generate again after reconnecting;
--   * the account's human tasks (reconnect prompts, permission decisions);
--   * the account row itself (seller id, region, scopes; the tokens are already NULL once
--     revoked, ck_ma_revoked_shredded).
-- What stays: audit_log rows (ids and allow-listed status values only, DATABASE.md §5.2), plus
-- one `marketplace_account.purged` tombstone in the tenant with the row counts and no seller data.
-- Both go with the tenant in purge_tenant.
-- It returns {row_counts, object_keys}: the submission payload and raw-response objects the caller
-- deletes from the submissions bucket before this transaction commits (a failed delete rolls the
-- rows back and the next daily run retries; a key already deleted counts as deleted).
-- Preconditions, GRD01 otherwise: the account is in tenant t; it is `revoked` since at least
-- 20 days (`revoked_at`) or `needs_reconnect` since at least 20 days (`needs_reconnect_since`).
-- With 7-day backups, 7 days of PITR logs and a 7-day bucket soft delete the data is gone by
-- about day 27, inside DPP 1.7's 30 days, with daily retries to spare (`force => true` skips the
-- age check only, for a deletion Amazon, Flipkart or the brand asks for). No job of those listings
-- may hold a live lease (`running` with `lease_expires_at` in the future); a stale `running` row
-- whose lease expired never blocks the purge. A queued job's row goes with its listing: the worker drops a queue entry whose row is
-- gone (the claim compare-and-set finds no row).
-- Migrator only (EXECUTE granted to nobody, SECURITY INVOKER, 0000 default privileges): it sets
-- app.purge_tenant to t for its transaction, the GUC the append-only, issue and task guards let
-- the migrator delete under, and defers the deferrable version pointers.
CREATE FUNCTION public.purge_marketplace_account(t uuid, a uuid, force boolean DEFAULT false)
  RETURNS jsonb
  LANGUAGE plpgsql SECURITY INVOKER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  acct record;
  doomed uuid[];
  subs uuid[];
  keys text[];
  n bigint;
  counts jsonb := '{}'::jsonb;
BEGIN
  SELECT x.marketplace, x.status, x.revoked_at, x.needs_reconnect_since INTO acct
    FROM public.marketplace_accounts x
   WHERE x.tenant_id = t AND x.id = a FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no marketplace account % in tenant %', a, t USING ERRCODE = 'GRD01';
  END IF;
  IF NOT coalesce(force, false) AND NOT coalesce(
       (acct.status = 'revoked' AND acct.revoked_at <= now() - interval '20 days')
       OR (acct.status = 'needs_reconnect'
           AND acct.needs_reconnect_since <= now() - interval '20 days'),
       false) THEN
    RAISE EXCEPTION 'purge_marketplace_account needs an account revoked or disconnected for 20 days (status %)',
      acct.status USING ERRCODE = 'GRD01';
  END IF;
  PERFORM set_config('app.purge_tenant', t::text, true);
  SET CONSTRAINTS ALL DEFERRED;

  SELECT coalesce(array_agg(l.id), ARRAY[]::uuid[]) INTO doomed
    FROM public.listings l
   WHERE l.tenant_id = t AND l.marketplace_account_id = a;
  PERFORM 1 FROM public.listings l WHERE l.id = ANY (doomed) FOR UPDATE;
  IF EXISTS (SELECT 1 FROM public.job_runs j
              WHERE j.tenant_id = t AND j.listing_id = ANY (doomed) AND j.status = 'running'
                AND j.lease_expires_at > now()) THEN
    RAISE EXCEPTION 'purge_marketplace_account: a job of the account''s listings holds a live lease'
      USING ERRCODE = 'GRD01';
  END IF;

  SELECT coalesce(array_agg(s.id), ARRAY[]::uuid[]),
         coalesce(array_agg(s.payload_storage_key), ARRAY[]::text[])
           || coalesce(array_agg(s.raw_response_storage_key)
                         FILTER (WHERE s.raw_response_storage_key IS NOT NULL), ARRAY[]::text[])
    INTO subs, keys
    FROM public.listing_submissions s
   WHERE s.tenant_id = t AND s.listing_id = ANY (doomed);

  DELETE FROM public.publish_field_results r
   WHERE r.tenant_id = t AND r.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('publish_field_results', n);
  DELETE FROM public.listing_submissions s WHERE s.tenant_id = t AND s.id = ANY (subs);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('listing_submissions', n);
  DELETE FROM public.listing_issues i WHERE i.tenant_id = t AND i.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('listing_issues', n);
  DELETE FROM public.human_tasks h WHERE h.tenant_id = t AND h.marketplace_account_id = a;
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('human_tasks', n);
  DELETE FROM public.listing_items i WHERE i.tenant_id = t AND i.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('listing_items', n);
  DELETE FROM public.review_decisions d WHERE d.tenant_id = t AND d.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('review_decisions', n);
  DELETE FROM public.validation_runs v WHERE v.tenant_id = t AND v.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('validation_runs', n);
  DELETE FROM public.agent_runs r WHERE r.tenant_id = t AND r.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('agent_runs', n);
  DELETE FROM public.performance p WHERE p.tenant_id = t AND p.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('performance', n);
  DELETE FROM public.job_runs j WHERE j.tenant_id = t AND j.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('job_runs', n);
  DELETE FROM public.listing_versions v WHERE v.tenant_id = t AND v.listing_id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('listing_versions', n);
  DELETE FROM public.listings l WHERE l.tenant_id = t AND l.id = ANY (doomed);
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('listings', n);
  DELETE FROM public.marketplace_accounts x WHERE x.tenant_id = t AND x.id = a;
  GET DIAGNOSTICS n = ROW_COUNT;
  counts := counts || jsonb_build_object('marketplace_accounts', n);

  INSERT INTO public.audit_log (tenant_id, actor_type, action, entity_type, entity_id, after)
  VALUES (t, 'system', 'marketplace_account.purged', 'marketplace_account', a,
          jsonb_build_object('marketplace', acct.marketplace, 'row_counts', counts,
                             'objects', cardinality(keys), 'forced', coalesce(force, false),
                             'backups_expire_by', now() + interval '7 days'));
  RETURN jsonb_build_object('row_counts', counts, 'object_keys', to_jsonb(keys));
END
$$;
