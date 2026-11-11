-- 0030/01_columns.up.sql (ADR-0031, owner decision 2026-11-11): the brand setting "Owners may
-- approve their own listings" and the mark on the approval it allowed.
-- 1. tenants.owner_self_approval, off by default (a catalog-only change on PostgreSQL 17, no
--    rewrite). app_rw may UPDATE it (the service lets only an active owner do so, after a
--    step-up); the read-only roles already SELECT every tenants column (0001).
ALTER TABLE public.tenants ADD COLUMN owner_self_approval boolean NOT NULL DEFAULT false;
GRANT UPDATE (owner_self_approval) ON public.tenants TO app_rw;

-- 2. listing_versions.self_approved: part of the one-time approval, written only by lv_guard
--    (02), which sets it to "the approver is in the contributor chain". app_rw holds neither
--    INSERT nor UPDATE on it (0004/04 grants are per column), so no caller can claim or hide a
--    self-approval; the read-only roles SELECT every listing_versions column (0002).
ALTER TABLE public.listing_versions ADD COLUMN self_approved boolean NOT NULL DEFAULT false;
ALTER TABLE public.listing_versions ADD CONSTRAINT ck_listing_versions_self_approved
  CHECK (NOT self_approved OR approved_by_user_id IS NOT NULL) NOT VALID;
ALTER TABLE public.listing_versions VALIDATE CONSTRAINT ck_listing_versions_self_approved;

-- 3. ck_maker_checker (0002) admits a self-approved row; lv_guard decides when one may exist.
--    Every existing row has self_approved false, so the new check holds where the old one did.
ALTER TABLE public.listing_versions DROP CONSTRAINT ck_maker_checker;
ALTER TABLE public.listing_versions ADD CONSTRAINT ck_maker_checker CHECK (
    approved_by_user_id IS NULL
    OR self_approved
    OR (approved_by_user_id <> generated_by_user_id
        AND approved_by_user_id IS DISTINCT FROM edited_by_user_id)
  ) NOT VALID;
ALTER TABLE public.listing_versions VALIDATE CONSTRAINT ck_maker_checker;
