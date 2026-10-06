-- 0004/15_privileges.down.sql
-- Back to the 0000 default privileges.
GRANT UPDATE, DELETE ON public.publish_field_results TO app_rw;
REVOKE UPDATE (outcome, error_code, error_report_json, submission_ref, raw_response_sha256,
               raw_response_storage_key, sent_at, responded_at, resolved_at)
  ON public.listing_submissions FROM app_rw;
REVOKE UPDATE (role, is_active, deactivated_at, last_used_at) ON public.tenant_memberships FROM app_rw;
REVOKE UPDATE (name, tone, guidelines, glossary) ON public.tenants FROM app_rw;
GRANT UPDATE, DELETE ON public.tenants, public.users, public.tenant_memberships,
  public.product_colourways, public.variant_matrix_cells, public.listing_submissions,
  public.brand_profiles, public.business_parties TO app_rw;
