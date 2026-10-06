-- 0004/14_platform_guards.down.sql
-- Back to the 0000 default privileges (SELECT, INSERT, UPDATE, DELETE; USAGE, SELECT on the
-- sequence) these tables were created with.
REVOKE INSERT (tenant_id, marketplace, category, field, rule_type, subject, pack, severity, version,
               enabled, rule_def) ON public.rules FROM app_rw;
REVOKE UPDATE (enabled) ON public.rules FROM app_rw;
GRANT INSERT, UPDATE, DELETE ON public.rules, public.category_templates, public.marketplace_error_map,
  public.attribute_definitions, public.attribute_columns, public.category_attributes TO app_rw;
DROP TRIGGER dictionary_guard ON public.category_attributes;
DROP TRIGGER dictionary_guard ON public.attribute_definitions;
DROP FUNCTION public.dictionary_guard();
DROP TRIGGER error_map_guard ON public.marketplace_error_map;
DROP FUNCTION public.error_map_guard();
DROP TRIGGER category_templates_guard ON public.category_templates;
DROP FUNCTION public.category_templates_guard();
DROP TRIGGER rules_guard ON public.rules;
DROP FUNCTION public.rules_guard();
