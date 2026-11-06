-- 0015/01_images_public.up.sql
-- SI1 (ADR-0026): the clean derivative and the public copy of an image. Expand only: four nullable
-- columns, their CHECKs, a cross-tenant unique public name and a delete guard (CP9) so a tenant
-- purge cannot drop the only record of a public object.
ALTER TABLE public.images
  ADD COLUMN clean_content_hash text,
  ADD COLUMN clean_mime_type text,
  ADD COLUMN public_id text,
  ADD COLUMN public_written_at timestamptz,
  ADD CONSTRAINT ck_images_clean_content_hash CHECK (clean_content_hash ~ '^[0-9a-f]{64}$'),
  ADD CONSTRAINT ck_images_clean_mime_type CHECK (clean_mime_type IN ('image/jpeg', 'image/png')),
  ADD CONSTRAINT ck_images_clean_pair CHECK ((clean_content_hash IS NULL) = (clean_mime_type IS NULL)),
  ADD CONSTRAINT ck_images_public_id CHECK (public_id ~ '^[a-z2-7]{26}$'),
  -- CP16: a deleted row keeps its public name as a tombstone after the clean pair is cleared
  ADD CONSTRAINT ck_images_public_needs_clean
    CHECK (public_id IS NULL OR clean_content_hash IS NOT NULL OR deleted_at IS NOT NULL),
  ADD CONSTRAINT ck_images_public_written CHECK (public_written_at IS NULL OR public_id IS NOT NULL);

CREATE UNIQUE INDEX images_public_id_uq ON public.images (public_id) WHERE public_id IS NOT NULL;

-- CP9: a purge cannot drop the only record of a public object
CREATE FUNCTION public.images_refuse_public_delete() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
AS $$
BEGIN
  RAISE EXCEPTION 'delete the public copy first (image %)', OLD.id USING ERRCODE = 'GRD01';
END
$$;

CREATE TRIGGER images_public_delete_guard BEFORE DELETE ON public.images
  FOR EACH ROW WHEN (OLD.public_id IS NOT NULL)
  EXECUTE FUNCTION public.images_refuse_public_delete();
