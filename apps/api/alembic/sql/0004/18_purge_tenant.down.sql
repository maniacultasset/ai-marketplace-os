-- 0004/18_purge_tenant.down.sql
DROP FUNCTION public.purge_tenant(uuid, text);
DROP FUNCTION public.purge_tenant_order();
