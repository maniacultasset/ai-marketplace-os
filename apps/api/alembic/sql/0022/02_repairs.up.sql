-- 0022/02_repairs.up.sql
-- SI11 (ADR-0028 proposed): an adapter-side repair of one marketplace attribute. It is adapter
-- data, never listing content: the adapter applies it only while the seller's source value still
-- hashes to `source_digest`, so a seller edit makes it stale (retired `source_changed`).
-- `source_issue_id` and `job_id` are soft references (no FK): job rows are pruned at 90 days and
-- an issue may be pruned with its listing's history, while a repair lives with its listing
-- (DATABASE.md "a row kept longer never holds a hard FK to a row pruned sooner"). The item and
-- listing FKs cascade, so the account purge (0011b) and the tenant purge never trip on a repair.
-- One live row per (listing, item, attribute); one try per (listing, issue key, rule), live or
-- retired (invariant 9's one-try backstop).
CREATE TABLE public.listing_repairs (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  listing_id uuid NOT NULL,
  marketplace text NOT NULL,
  listing_item_id uuid,
  field_path text NOT NULL,
  attribute text NOT NULL,
  rule text NOT NULL,
  issue_key text NOT NULL,
  source_issue_id uuid,
  job_id uuid,
  source_digest bytea NOT NULL,
  to_digest bytea NOT NULL,
  value_json jsonb,
  alias_version smallint,
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz,
  retired_reason text,
  CONSTRAINT pk_listing_repairs PRIMARY KEY (id),
  CONSTRAINT uq_listing_repairs_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_listing_repairs_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT fk_listing_repairs_listing_id FOREIGN KEY (tenant_id, listing_id)
    REFERENCES public.listings (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_listing_repairs_listing_id_marketplace FOREIGN KEY (listing_id, marketplace)
    REFERENCES public.listings (id, marketplace) ON DELETE CASCADE,
  CONSTRAINT fk_listing_repairs_listing_item_id_listing_id FOREIGN KEY (listing_item_id, listing_id)
    REFERENCES public.listing_items (id, listing_id) ON DELETE CASCADE,
  CONSTRAINT ck_listing_repairs_marketplace CHECK (marketplace IN ('amazon', 'flipkart')),
  CONSTRAINT ck_listing_repairs_field_path CHECK (char_length(field_path) BETWEEN 1 AND 192),
  CONSTRAINT ck_listing_repairs_attribute CHECK (attribute ~ '^[a-z0-9_]{1,128}$'),
  CONSTRAINT ck_listing_repairs_rule CHECK (rule IN (
    'enum_fold', 'unit_format', 'identifier_normalize', 'exemption_flag', 'fill_from_facts',
    'drop_locked', 'image_slot')),
  CONSTRAINT ck_listing_repairs_issue_key CHECK (char_length(issue_key) BETWEEN 1 AND 400),
  CONSTRAINT ck_listing_repairs_digests
    CHECK (octet_length(source_digest) = 32 AND octet_length(to_digest) = 32),
  CONSTRAINT ck_listing_repairs_value_json CHECK (
    (value_json IS NULL) = (rule = 'drop_locked')
    AND (value_json IS NULL OR octet_length(value_json::text) <= 2048)),
  CONSTRAINT ck_listing_repairs_alias_version
    CHECK (alias_version IS NULL OR (rule = 'enum_fold' AND alias_version > 0)),
  CONSTRAINT ck_listing_repairs_retired CHECK (
    (retired_at IS NULL) = (retired_reason IS NULL)
    AND (retired_reason IS NULL
         OR retired_reason IN ('source_changed', 'superseded', 'regenerated')))
);

CREATE UNIQUE INDEX uq_listing_repairs_live
  ON public.listing_repairs (listing_id, listing_item_id, attribute) NULLS NOT DISTINCT
  WHERE retired_at IS NULL;
CREATE UNIQUE INDEX uq_listing_repairs_once
  ON public.listing_repairs (listing_id, issue_key, rule);
CREATE INDEX ix_listing_repairs_listing
  ON public.listing_repairs (tenant_id, listing_id, created_at DESC);

-- A repair retires once; nothing else about it changes (app_rw has no other column).
CREATE FUNCTION public.listing_repairs_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF OLD.retired_at IS NOT NULL THEN
    RAISE EXCEPTION 'a listing repair retires once' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER listing_repairs_guard BEFORE UPDATE ON public.listing_repairs
  FOR EACH ROW EXECUTE FUNCTION public.listing_repairs_guard();

REVOKE UPDATE, DELETE, TRUNCATE ON public.listing_repairs FROM app_rw;
GRANT UPDATE (retired_at, retired_reason) ON public.listing_repairs TO app_rw;
-- value_json holds seller fact values: never granted to the read-only roles.
GRANT SELECT (id, tenant_id, listing_id, marketplace, listing_item_id, field_path, attribute,
              rule, issue_key, source_issue_id, job_id, source_digest, to_digest, alias_version,
              created_at, retired_at, retired_reason)
  ON public.listing_repairs TO app_ro, app_platform_ro;

ALTER TABLE public.listing_repairs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.listing_repairs FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.listing_repairs TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
CREATE POLICY t_platform ON public.listing_repairs FOR SELECT TO app_platform_ro, app_ro
  USING ((current_user IN ('app_platform_ro', 'app_ro')));
CREATE POLICY t_migrator ON public.listing_repairs TO migrator USING (true) WITH CHECK (true);

-- audit_allowlist is 0018's body plus listing_repair (rule and attribute only);
-- purge_tenant_order() is 0018's array with listing_repairs ahead of listing_items.
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
    ELSE public.audit_unknown_entity(p_entity_type)
  END;
END
$$;

CREATE OR REPLACE FUNCTION public.purge_tenant_order() RETURNS text[]
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
  RETURN ARRAY[
    'image_candidates', 'listing_repairs', 'publish_field_results', 'listing_submissions', 'listing_issues',
    'human_tasks',
    'listing_items', 'review_decisions', 'validation_runs', 'agent_runs', 'performance',
    'import_rows', 'imports', 'idempotency_keys', 'job_runs', 'batches', 'listing_versions',
    'listings', 'oauth_states', 'refresh_tokens', 'variant_matrix_cells', 'images',
    'product_colourways', 'brand_chunks', 'brand_documents', 'marketplace_accounts', 'products',
    'brand_profiles', 'business_parties', 'password_reset_links', 'invitations', 'rules'];
END;
