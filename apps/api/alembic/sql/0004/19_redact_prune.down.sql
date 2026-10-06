-- 0004/19_redact_prune.down.sql
DROP FUNCTION public.prune_closed(regclass, timestamptz);
DROP FUNCTION public.prune_append_only(regclass, timestamptz);
DROP FUNCTION public.prune_audit_log(timestamptz);
DROP FUNCTION public.redact_user(uuid);
