-- 0034/01_sku_counters.up.sql (client feedback 11 Nov, item 3: "Make one for me"): the last
-- number handed out per SKU code of a tenant. `code` is `<BRAND>-<CATEGORY>` (the brand prefix and
-- the category code, upper-case letters and digits); the made SKU is `<code>-<NNNN>`. The number
-- only grows (one atomic upsert per suggestion, so two requests never get the same one), so a SKU
-- once offered is never offered again, even when it was never saved or its product was deleted.
-- app_rw holds no DELETE: a counter lives until the tenant purge. No seller text is stored.
CREATE TABLE public.sku_counters (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  code text NOT NULL,
  last_value bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_sku_counters PRIMARY KEY (id),
  CONSTRAINT uq_sku_counters_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT uq_sku_counters_tenant_id_code UNIQUE (tenant_id, code),
  CONSTRAINT fk_sku_counters_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT ck_sku_counters_code CHECK (code ~ '^[A-Z0-9]{1,6}-[A-Z0-9]{1,6}$'),
  CONSTRAINT ck_sku_counters_last_value CHECK (last_value BETWEEN 1 AND 99999999)
);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.sku_counters
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

REVOKE DELETE, TRUNCATE ON public.sku_counters FROM app_rw;
GRANT SELECT (id, tenant_id, code, last_value, created_at, updated_at)
  ON public.sku_counters TO app_ro, app_platform_ro;

ALTER TABLE public.sku_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sku_counters FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.sku_counters TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
CREATE POLICY t_platform ON public.sku_counters FOR SELECT TO app_platform_ro, app_ro
  USING ((current_user IN ('app_platform_ro', 'app_ro')));
CREATE POLICY t_migrator ON public.sku_counters TO migrator USING (true) WITH CHECK (true);
