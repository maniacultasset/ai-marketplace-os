-- 0004/04_lv_guard.down.sql
REVOKE UPDATE (approved_by_user_id, approved_at) ON public.listing_versions FROM app_rw;
GRANT UPDATE, DELETE ON public.listing_versions TO app_rw;
DROP TRIGGER lv_guard ON public.listing_versions;
DROP FUNCTION public.lv_guard();
