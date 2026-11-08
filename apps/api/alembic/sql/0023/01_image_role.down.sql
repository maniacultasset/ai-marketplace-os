-- 0023/01_image_role.down.sql
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.images WHERE role IS NOT NULL) THEN
    RAISE EXCEPTION '0023 downgrade refused: images carry a role';
  END IF;
END
$$;
DROP INDEX public.images_one_role;
ALTER TABLE public.images DROP CONSTRAINT ck_images_role;
ALTER TABLE public.images DROP COLUMN role;
