-- 0017/02_tenant_recovery.down.sql
DROP POLICY tenants_platform_admin ON public.tenants;
DROP FUNCTION public.extend_tenant_trial(uuid, integer, text, text);
DROP FUNCTION public.activate_tenant(uuid, text, text);
