-- 0040/01_photo_links.up.sql
-- G2 (photos from spreadsheet links): one row per photo link a bulk import named for a product
-- (and, on a style split by colour, a colourway). The import writes the rows `queued` and never
-- opens a link; a background `image_compliance` job about a `photo_links` entity downloads them
-- and stores each through the upload path (`images` + its checks), then marks the row `saved`
-- with the image, or `failed` with a closed `problem`.
--
-- `url` is the seller's link as typed; `url_hash` is sha256 of the direct-download form the
-- fetcher opens (a Drive or Dropbox share link rewritten). A link can carry a signed token, so
-- neither is granted to the read roles, and `images.source_url` is never written from it.
-- `job_id` and `import_id` have no FK: job rows are pruned at 90 days and imports at 90 days,
-- while a link lives with its product (DATABASE.md "a row kept longer never holds a hard FK to a
-- row pruned sooner"). `image_id` keeps its FK: images are soft-deleted, and the purge removes
-- photo_links first (0040/03).
CREATE TABLE public.photo_links (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  product_id uuid NOT NULL,
  colourway_id uuid,
  url text NOT NULL,
  url_hash text NOT NULL,
  source_column text NOT NULL,
  position smallint NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  problem text,
  http_status smallint,
  file_type text,
  image_id uuid,
  import_id uuid,
  row_number integer,
  requested_by uuid,
  job_id uuid,
  attempts smallint NOT NULL DEFAULT 0,
  fetched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_photo_links PRIMARY KEY (id),
  CONSTRAINT uq_photo_links_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_photo_links_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT fk_photo_links_product_id FOREIGN KEY (tenant_id, product_id)
    REFERENCES public.products (tenant_id, id),
  CONSTRAINT fk_photo_links_colourway_id FOREIGN KEY (tenant_id, colourway_id)
    REFERENCES public.product_colourways (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_photo_links_colourway_id_product_id FOREIGN KEY (colourway_id, product_id)
    REFERENCES public.product_colourways (id, style_product_id) ON DELETE RESTRICT,
  CONSTRAINT fk_photo_links_image_id FOREIGN KEY (tenant_id, image_id)
    REFERENCES public.images (tenant_id, id),
  CONSTRAINT fk_photo_links_requested_by FOREIGN KEY (tenant_id, requested_by)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT ck_photo_links_url CHECK (char_length(url) BETWEEN 9 AND 2048 AND url ~ '^https://'),
  CONSTRAINT ck_photo_links_url_hash CHECK (url_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_photo_links_source_column
    CHECK (source_column IN ('main_image', 'additional_images')),
  CONSTRAINT ck_photo_links_position CHECK (position BETWEEN 1 AND 9),
  CONSTRAINT ck_photo_links_status
    CHECK (status IN ('queued', 'fetching', 'saved', 'failed', 'skipped', 'dismissed')),
  CONSTRAINT ck_photo_links_problem CHECK (problem IN (
    'not_found', 'not_public', 'not_image', 'unsupported_type', 'too_large', 'too_many_pixels',
    'corrupt', 'timeout', 'unreachable', 'blocked_address', 'too_many_redirects', 'server_error',
    'photo_limit', 'import_cap', 'removed_by_seller', 'product_gone')),
  CONSTRAINT ck_photo_links_problem_needed
    CHECK (status NOT IN ('failed', 'skipped') OR problem IS NOT NULL),
  CONSTRAINT ck_photo_links_problem_none
    CHECK (status NOT IN ('queued', 'fetching', 'saved') OR problem IS NULL),
  CONSTRAINT ck_photo_links_saved CHECK (status <> 'saved' OR image_id IS NOT NULL),
  CONSTRAINT ck_photo_links_http_status CHECK (http_status BETWEEN 100 AND 599),
  -- `unsupported_type`: the format found, so the seller is told which one
  CONSTRAINT ck_photo_links_file_type CHECK (file_type IN ('gif', 'webp', 'tiff', 'heic', 'bmp')),
  CONSTRAINT ck_photo_links_row_number CHECK (row_number >= 1),
  CONSTRAINT ck_photo_links_attempts CHECK (attempts >= 0)
);

-- one row per link per target: S/M/L rows of one colour that share a link fetch it once
CREATE UNIQUE INDEX uq_photo_links_dedupe
  ON public.photo_links (tenant_id, product_id, colourway_id, url_hash) NULLS NOT DISTINCT;
CREATE INDEX ix_photo_links_import_rows
  ON public.photo_links (tenant_id, import_id, row_number);
CREATE INDEX ix_photo_links_waiting
  ON public.photo_links (tenant_id, product_id) WHERE status IN ('queued', 'fetching');

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.photo_links
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- app_rw changes only the outcome and provenance columns; the target and the link are write-once.
REVOKE UPDATE, TRUNCATE ON public.photo_links FROM app_rw;
GRANT UPDATE (status, problem, http_status, file_type, image_id, import_id, row_number, position,
              source_column, requested_by, job_id, attempts, fetched_at, updated_at)
  ON public.photo_links TO app_rw;
-- The link may carry a signed token, and url_hash matches the §8.2 secret pattern: both denied.
GRANT SELECT (id, tenant_id, product_id, colourway_id, source_column, position, status, problem,
              http_status, file_type, image_id, import_id, row_number, requested_by, job_id, attempts,
              fetched_at, created_at, updated_at)
  ON public.photo_links TO app_ro, app_platform_ro;

ALTER TABLE public.photo_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.photo_links FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.photo_links TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
CREATE POLICY t_platform ON public.photo_links FOR SELECT TO app_platform_ro, app_ro
  USING ((current_user IN ('app_platform_ro', 'app_ro')));
CREATE POLICY t_migrator ON public.photo_links TO migrator USING (true) WITH CHECK (true);
