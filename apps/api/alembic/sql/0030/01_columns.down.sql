-- 0030/01_columns.down.sql (local and CI only): refused while a self-approval is recorded, so a
-- downgrade never erases one; then restores 0002's ck_maker_checker and drops the two columns
-- (the column grant goes with its column).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.listing_versions WHERE self_approved) THEN
    RAISE EXCEPTION '0030 downgrade refused: a self-approval is recorded'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
ALTER TABLE public.listing_versions DROP CONSTRAINT ck_maker_checker;
ALTER TABLE public.listing_versions ADD CONSTRAINT ck_maker_checker CHECK (
    approved_by_user_id IS NULL
    OR (approved_by_user_id <> generated_by_user_id
        AND approved_by_user_id IS DISTINCT FROM edited_by_user_id)
  );
ALTER TABLE public.listing_versions DROP CONSTRAINT ck_listing_versions_self_approved;
ALTER TABLE public.listing_versions DROP COLUMN self_approved;
ALTER TABLE public.tenants DROP COLUMN owner_self_approval;
