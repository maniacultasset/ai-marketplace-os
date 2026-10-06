-- 0004/16_tenant_lifecycle.down.sql
DROP FUNCTION public.expire_trials();
DROP FUNCTION public.set_tenant_lifecycle(uuid, text, text);
