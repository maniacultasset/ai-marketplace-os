-- 0018/02_images_provenance.up.sql
-- SI7: where an image's pixels came from. `origin` has a constant default, so the column add is
-- metadata only on PG17. `candidate_id` has no FK (CP-D3): image_candidates already references
-- images, and an FK both ways leaves no purge order. Provenance is write-once: the guard refuses
-- any change except the parent FK's own ON DELETE SET NULL.
ALTER TABLE public.images
  ADD COLUMN origin text NOT NULL DEFAULT 'seller',
  ADD COLUMN parent_image_id uuid,
  ADD COLUMN candidate_id uuid,
  ADD CONSTRAINT ck_images_origin CHECK (origin IN ('seller', 'generated', 'rendered')),
  ADD CONSTRAINT ck_images_origin_candidate CHECK ((origin = 'seller') = (candidate_id IS NULL)),
  ADD CONSTRAINT fk_images_parent_image_id FOREIGN KEY (tenant_id, parent_image_id)
    REFERENCES public.images (tenant_id, id) ON DELETE SET NULL (parent_image_id);

GRANT SELECT (origin, parent_image_id, candidate_id) ON public.images TO app_ro, app_platform_ro;

CREATE FUNCTION public.images_provenance_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NEW.origin IS DISTINCT FROM OLD.origin
     OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
     OR (NEW.parent_image_id IS DISTINCT FROM OLD.parent_image_id
         AND NEW.parent_image_id IS NOT NULL) THEN
    RAISE EXCEPTION 'image provenance is write-once (image %)', OLD.id USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER images_provenance_guard BEFORE UPDATE ON public.images
  FOR EACH ROW EXECUTE FUNCTION public.images_provenance_guard();
