-- 0023/01_image_role.up.sql
-- SI12: the seller's Back and Close-up photos are `secondary` images with a role (Front stays
-- `main`). The column add is metadata only; the CHECK goes in NOT VALID and is validated under
-- SHARE UPDATE EXCLUSIVE. One live holder per (product, colourway, role), as `images_one_main`.
ALTER TABLE public.images ADD COLUMN role text;
ALTER TABLE public.images ADD CONSTRAINT ck_images_role
  CHECK (role IS NULL OR (slot = 'secondary' AND role IN ('back', 'close_up')))
  NOT VALID;
ALTER TABLE public.images VALIDATE CONSTRAINT ck_images_role;
-- squawk require-concurrent-index-creation: accepted as for images_one_main (0002). The index is
-- built in the migration's transaction because images is small in V1.
CREATE UNIQUE INDEX images_one_role ON public.images (product_id, colourway_id, role)
  NULLS NOT DISTINCT WHERE role IS NOT NULL AND deleted_at IS NULL;
