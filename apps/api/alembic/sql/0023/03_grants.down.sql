-- 0023/03_grants.down.sql
REVOKE SELECT (role) ON public.images FROM app_ro, app_platform_ro;
