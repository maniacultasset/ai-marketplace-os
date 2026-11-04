-- 0014/01_product_type_definitions.up.sql
-- DW2 (T53): Amazon Product Type Definitions, stored versioned. Platform-global like
-- category_templates (DATABASE.md:158): no tenant_id, no RLS, SELECT only for the app roles,
-- written only by the migrator's `app.cli templates refresh|promote-definition`, audited with a
-- NULL tenant. The pre-signed schema link is never stored (meta_schema_id refuses a query or
-- fragment) and no column names a person: the actor lives in the audit row.
CREATE TABLE public.product_type_definitions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  marketplace text NOT NULL,
  marketplace_id text NOT NULL,
  product_type text NOT NULL,
  requirements text NOT NULL,
  locale text NOT NULL,
  product_type_version text NOT NULL,
  requirements_enforced text NOT NULL,
  display_name text,
  property_groups jsonb NOT NULL,
  schema_json jsonb NOT NULL,
  meta_schema_id text,
  schema_digest bytea NOT NULL,
  fetched_at timestamptz NOT NULL,
  is_current boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_product_type_definitions PRIMARY KEY (id),
  CONSTRAINT ptd_version_key UNIQUE (marketplace_id, product_type, requirements, locale,
                                     product_type_version, schema_digest),
  CONSTRAINT ck_product_type_definitions_marketplace CHECK (marketplace = 'amazon'),
  CONSTRAINT ck_product_type_definitions_marketplace_id CHECK (marketplace_id ~ '^[A-Z0-9]{1,32}$'),
  CONSTRAINT ck_product_type_definitions_product_type CHECK (product_type ~ '^[A-Z0-9_]{1,64}$'),
  CONSTRAINT ck_product_type_definitions_requirements
    CHECK (requirements IN ('LISTING', 'LISTING_PRODUCT_ONLY', 'LISTING_OFFER_ONLY')),
  CONSTRAINT ck_product_type_definitions_locale CHECK (locale ~ '^[a-z]{2}_[A-Z]{2}$'),
  CONSTRAINT ck_product_type_definitions_version
    CHECK (char_length(product_type_version) BETWEEN 1 AND 128),
  CONSTRAINT ck_product_type_definitions_enforced
    CHECK (requirements_enforced IN ('ENFORCED', 'NOT_ENFORCED')),
  CONSTRAINT ck_product_type_definitions_display_name
    CHECK (display_name IS NULL OR char_length(display_name) <= 256),
  CONSTRAINT ck_product_type_definitions_schema_json CHECK (jsonb_typeof(schema_json) = 'object'),
  CONSTRAINT ck_product_type_definitions_meta_schema_id
    CHECK (meta_schema_id IS NULL
           OR (char_length(meta_schema_id) <= 512 AND meta_schema_id !~ '[?#]')),
  CONSTRAINT ck_product_type_definitions_schema_digest CHECK (octet_length(schema_digest) = 32)
);

CREATE UNIQUE INDEX ptd_one_current
  ON public.product_type_definitions (marketplace_id, product_type, requirements, locale)
  WHERE is_current;

-- Content is immutable for every role, the migrator included: only is_current moves, and no
-- row is ever deleted (the category_templates_guard pattern, 0004/14_platform_guards).
CREATE FUNCTION public.product_type_definitions_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'product type definitions are never deleted' USING ERRCODE = 'GRD01';
  END IF;
  IF (to_jsonb(NEW) - 'is_current') IS DISTINCT FROM (to_jsonb(OLD) - 'is_current') THEN
    RAISE EXCEPTION 'a product type definition''s content is immutable; only is_current moves'
      USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER product_type_definitions_guard
  BEFORE UPDATE OR DELETE ON public.product_type_definitions
  FOR EACH ROW EXECUTE FUNCTION public.product_type_definitions_guard();

-- app_rw gets DML through the 0000 default privileges; take it back (0004/14 pattern).
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.product_type_definitions FROM app_rw;
GRANT SELECT ON public.product_type_definitions TO app_ro, app_platform_ro;
