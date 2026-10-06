-- 0004/05_last_owner.down.sql
DROP TRIGGER last_owner_guard ON public.tenant_memberships;
DROP FUNCTION public.last_owner_guard();
