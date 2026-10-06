-- 0004/02_audit.down.sql
DROP FUNCTION public.audit(uuid, text, uuid, text, text, uuid, jsonb, jsonb, text, text, uuid, inet, text);
DROP FUNCTION public.audit_allowlist(text);
DROP FUNCTION public.audit_unknown_entity(text);
