-- 0004/04_lv_guard.down.sql
REVOKE UPDATE (approved_by_user_id, approved_at) ON public.listing_versions FROM app_rw;
REVOKE INSERT (id, tenant_id, listing_id, version, content_json, image_reports_json,
               generated_by_user_id, edited_by_user_id, origin, parent_version_id, changed_paths,
               locale, rules_version, category_template_id, job_id, attempts_used, created_at)
  ON public.listing_versions FROM app_rw;
GRANT INSERT, UPDATE, DELETE ON public.listing_versions TO app_rw;
DROP TRIGGER lv_guard ON public.listing_versions;
DROP FUNCTION public.lv_guard();
