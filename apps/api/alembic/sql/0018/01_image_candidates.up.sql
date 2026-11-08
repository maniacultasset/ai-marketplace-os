-- 0018/01_image_candidates.up.sql
-- SI7 (ADR-0030 proposed): a proposed product image (a white-background edit, a deterministic
-- re-frame or a size-chart render). It becomes an `images` row only when the seller accepts it.
-- `job_id` has no FK (job rows are pruned); the cache index leaves out the retryable failures, so
-- a seller can try again after an outage, a refused budget or a source that changed or went away
-- (`source_changed`), never after a verdict on the pixels.
CREATE TABLE public.image_candidates (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  product_id uuid NOT NULL,
  colourway_id uuid,
  source_image_id uuid,
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  failure_reason text,
  source_hash text NOT NULL,
  generator_key text NOT NULL,
  storage_key text,
  mime_type text,
  width_px integer,
  height_px integer,
  fidelity_json jsonb,
  served_provider text,
  agent_run_id uuid,
  cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
  job_id uuid,
  accepted_image_id uuid,
  created_by uuid,
  decided_by uuid,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_image_candidates PRIMARY KEY (id),
  CONSTRAINT uq_image_candidates_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_image_candidates_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT fk_image_candidates_product_id FOREIGN KEY (tenant_id, product_id)
    REFERENCES public.products (tenant_id, id),
  CONSTRAINT fk_image_candidates_colourway_id FOREIGN KEY (tenant_id, colourway_id)
    REFERENCES public.product_colourways (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_image_candidates_colourway_id_product_id FOREIGN KEY (colourway_id, product_id)
    REFERENCES public.product_colourways (id, style_product_id) ON DELETE RESTRICT,
  CONSTRAINT fk_image_candidates_source_image_id FOREIGN KEY (tenant_id, source_image_id)
    REFERENCES public.images (tenant_id, id),
  CONSTRAINT fk_image_candidates_agent_run_id FOREIGN KEY (tenant_id, agent_run_id)
    REFERENCES public.agent_runs (tenant_id, id),
  CONSTRAINT fk_image_candidates_accepted_image_id FOREIGN KEY (tenant_id, accepted_image_id)
    REFERENCES public.images (tenant_id, id),
  CONSTRAINT fk_image_candidates_created_by FOREIGN KEY (tenant_id, created_by)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT fk_image_candidates_decided_by FOREIGN KEY (tenant_id, decided_by)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT ck_image_candidates_kind CHECK (kind IN ('white_background', 'fix', 'size_chart')),
  CONSTRAINT ck_image_candidates_status
    CHECK (status IN ('queued', 'ready', 'failed', 'accepted', 'rejected')),
  CONSTRAINT ck_image_candidates_failure_reason CHECK (failure_reason IN (
    'fidelity', 'not_white', 'too_small', 'source_unusable', 'no_size_chart', 'model_failed',
    'credit_used_up', 'budget', 'source_changed')),
  CONSTRAINT ck_image_candidates_failed CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
  CONSTRAINT ck_image_candidates_source CHECK ((kind = 'size_chart') = (source_image_id IS NULL)),
  CONSTRAINT ck_image_candidates_source_hash CHECK (source_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_image_candidates_generator_key
    CHECK (char_length(generator_key) BETWEEN 1 AND 200),
  CONSTRAINT ck_image_candidates_mime_type CHECK (mime_type = 'image/jpeg'),
  CONSTRAINT ck_image_candidates_dimensions CHECK (width_px > 0 AND height_px > 0),
  CONSTRAINT ck_image_candidates_fidelity_json CHECK (jsonb_typeof(fidelity_json) = 'object'),
  CONSTRAINT ck_image_candidates_served_provider
    CHECK (char_length(served_provider) BETWEEN 1 AND 64),
  CONSTRAINT ck_image_candidates_stored
    CHECK (status NOT IN ('ready', 'accepted', 'rejected') OR storage_key IS NOT NULL),
  CONSTRAINT ck_image_candidates_cost_usd CHECK (cost_usd >= 0),
  CONSTRAINT ck_image_candidates_accepted
    CHECK ((status = 'accepted') = (accepted_image_id IS NOT NULL))
);

CREATE UNIQUE INDEX uq_image_candidates_cache
  ON public.image_candidates (tenant_id, product_id, kind, source_hash, generator_key)
  WHERE NOT (status = 'failed'
    AND failure_reason IN ('model_failed', 'credit_used_up', 'budget', 'source_changed'));
CREATE INDEX ix_image_candidates_product
  ON public.image_candidates (tenant_id, product_id, created_at DESC);
CREATE INDEX ix_image_candidates_tenant_id_created_at
  ON public.image_candidates (tenant_id, created_at);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.image_candidates
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- app_rw changes only the outcome columns; kind, source and cache key are write-once.
REVOKE UPDATE, TRUNCATE ON public.image_candidates FROM app_rw;
GRANT UPDATE (status, failure_reason, storage_key, mime_type, width_px, height_px, fidelity_json,
              served_provider, agent_run_id, cost_usd, job_id, accepted_image_id, decided_by,
              decided_at, updated_at)
  ON public.image_candidates TO app_rw;
-- §8.2 secret pattern `(token|hash|password|secret|verifier|_enc)$`: source_hash stays denied.
GRANT SELECT (id, tenant_id, product_id, colourway_id, source_image_id, kind, status,
              failure_reason, generator_key, storage_key, mime_type, width_px, height_px,
              fidelity_json, served_provider, agent_run_id, cost_usd, job_id, accepted_image_id,
              created_by, decided_by, decided_at, created_at, updated_at)
  ON public.image_candidates TO app_ro, app_platform_ro;

ALTER TABLE public.image_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.image_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.image_candidates TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
CREATE POLICY t_platform ON public.image_candidates FOR SELECT TO app_platform_ro, app_ro
  USING ((current_user IN ('app_platform_ro', 'app_ro')));
CREATE POLICY t_migrator ON public.image_candidates TO migrator USING (true) WITH CHECK (true);
