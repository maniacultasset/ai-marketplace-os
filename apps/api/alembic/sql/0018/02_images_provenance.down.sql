-- 0018/02_images_provenance.down.sql
DROP TRIGGER images_provenance_guard ON public.images;
DROP FUNCTION public.images_provenance_guard();
ALTER TABLE public.images
  DROP CONSTRAINT fk_images_parent_image_id,
  DROP CONSTRAINT ck_images_origin_candidate,
  DROP CONSTRAINT ck_images_origin,
  DROP COLUMN candidate_id,
  DROP COLUMN parent_image_id,
  DROP COLUMN origin;
