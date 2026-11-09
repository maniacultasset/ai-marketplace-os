-- 0027/05_import_column_maps.up.sql (SI14, AC7-AC8): a seller's spreadsheet header mapped onto the
-- importer's accepted header names before the upload. `samples` are seller cells kept for the
-- seller's own review only: never sent to a model, never granted to app_ro or app_platform_ro,
-- cleared at confirm or after 7 days (retention). `job_id` is a soft reference (jobs are pruned
-- at 90 days). app_rw holds no DELETE: a map lives until the tenant purge.
CREATE TABLE public.import_column_maps (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  status text NOT NULL,
  headers text[] NOT NULL,
  headers_digest text NOT NULL,
  mappings jsonb NOT NULL,
  samples jsonb,
  job_id uuid,
  created_by_user_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz,
  CONSTRAINT pk_import_column_maps PRIMARY KEY (id),
  CONSTRAINT uq_import_column_maps_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_import_column_maps_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT fk_import_column_maps_created_by_user_id FOREIGN KEY (tenant_id, created_by_user_id)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT ck_import_column_maps_status
    CHECK (status IN ('mapping', 'ready', 'confirmed', 'failed')),
  CONSTRAINT ck_import_column_maps_headers CHECK (
    array_ndims(headers) = 1 AND cardinality(headers) BETWEEN 1 AND 200
    AND array_position(headers, NULL) IS NULL),
  CONSTRAINT ck_import_column_maps_headers_digest CHECK (headers_digest ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_import_column_maps_mappings CHECK (
    jsonb_typeof(mappings) = 'array' AND jsonb_array_length(mappings) <= 200),
  CONSTRAINT ck_import_column_maps_samples CHECK (samples IS NULL OR jsonb_typeof(samples) = 'object'),
  CONSTRAINT ck_import_column_maps_confirmed CHECK ((status = 'confirmed') = (confirmed_at IS NOT NULL))
);
CREATE INDEX ix_import_column_maps_tenant_id_created_at
  ON public.import_column_maps (tenant_id, created_at DESC);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.import_column_maps
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

REVOKE DELETE, TRUNCATE ON public.import_column_maps FROM app_rw;
GRANT SELECT (id, tenant_id, status, headers, headers_digest, mappings, job_id,
              created_by_user_id, created_at, updated_at, confirmed_at)
  ON public.import_column_maps TO app_ro;

ALTER TABLE public.import_column_maps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.import_column_maps FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.import_column_maps TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
-- `t_app_ro`, not `t_platform`: the samples stay off app_platform_ro, so only app_ro reads here
CREATE POLICY t_app_ro ON public.import_column_maps FOR SELECT TO app_ro
  USING ((current_user = 'app_ro'));
CREATE POLICY t_migrator ON public.import_column_maps TO migrator USING (true) WITH CHECK (true);
