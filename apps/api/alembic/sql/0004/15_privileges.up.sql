-- 0004/15_privileges.up.sql
-- The remaining app_rw REVOKEs and column grants, each from its table's Privileges bullet in
-- DATABASE.md (the 0000 default privileges grant SELECT, INSERT, UPDATE, DELETE on every table):
--   tenants (:229)               no DELETE; UPDATE only name, tone, guidelines, glossary (the
--                                lifecycle columns move only through 16_tenant_lifecycle);
--   users (:244)                 no DELETE;
--   tenant_memberships           no DELETE; UPDATE only role, is_active, deactivated_at,
--                                last_used_at (DATABASE.md gives no list: the smallest set the
--                                members API and login need);
--   product_colourways (:438)    SELECT, INSERT, UPDATE;
--   variant_matrix_cells (:453)  no DELETE;
--   listing_submissions (:881)   INSERT and SELECT, plus UPDATE of the outcome columns;
--   brand_profiles (:1134),
--   business_parties (:1150)     no DELETE.
-- Earlier files hold the others: listing_versions (04), listing_items (11), listing_issues and
-- human_tasks (12), the append-only tables (03), the platform tables and rules (01, 14).
REVOKE DELETE, TRUNCATE ON public.tenants, public.users, public.tenant_memberships,
  public.product_colourways, public.variant_matrix_cells, public.listing_submissions,
  public.brand_profiles, public.business_parties FROM app_rw;
REVOKE UPDATE ON public.tenants, public.tenant_memberships, public.listing_submissions FROM app_rw;
GRANT UPDATE (name, tone, guidelines, glossary) ON public.tenants TO app_rw;
GRANT UPDATE (role, is_active, deactivated_at, last_used_at) ON public.tenant_memberships TO app_rw;
GRANT UPDATE (outcome, error_code, error_report_json, submission_ref, raw_response_sha256,
              raw_response_storage_key, sent_at, responded_at, resolved_at)
  ON public.listing_submissions TO app_rw;
