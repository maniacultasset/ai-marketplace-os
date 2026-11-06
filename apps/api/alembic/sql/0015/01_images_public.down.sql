-- 0015/01_images_public.down.sql (local and CI only; no prod downgrade)
DROP TRIGGER images_public_delete_guard ON public.images;
DROP FUNCTION public.images_refuse_public_delete();
DROP INDEX public.images_public_id_uq;
ALTER TABLE public.images
  DROP CONSTRAINT ck_images_public_written,
  DROP CONSTRAINT ck_images_public_needs_clean,
  DROP CONSTRAINT ck_images_public_id,
  DROP CONSTRAINT ck_images_clean_pair,
  DROP CONSTRAINT ck_images_clean_mime_type,
  DROP CONSTRAINT ck_images_clean_content_hash,
  DROP COLUMN public_written_at,
  DROP COLUMN public_id,
  DROP COLUMN clean_mime_type,
  DROP COLUMN clean_content_hash;
