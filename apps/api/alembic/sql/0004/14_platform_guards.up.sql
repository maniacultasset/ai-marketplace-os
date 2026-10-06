-- 0004/14_platform_guards.up.sql
-- DATABASE.md §5.2 rules "Guard" and "Privileges and RLS" (:504, :508), category_templates
-- "Guard" (:543), marketplace_error_map (:555), §5.6 attribute_definitions (:1153) and
-- category_attributes (:1176). Refusals → GRD01.
--   rules:                 only `enabled` changes; every UPDATE takes a new revision from
--                          rules_revision_seq; DELETE only by the migrator under the purge GUC.
--   category_templates:    content immutable, only `is_current` moves (promotion = two ordered
--                          UPDATEs); no DELETE (validation runs pin rows forever).
--   marketplace_error_map: only `enabled` changes; no DELETE.
--   attribute_definitions,
--   category_attributes:   a new revision only when the row (minus `revision`) changed, so a
--                          no-op `dictionary sync` upsert keeps it (and `rules_version`); no DELETE.
-- Privileges: the platform tables are SELECT-only for app_rw; `rules` keeps column INSERT, UPDATE
-- (enabled) and USAGE on rules_revision_seq for brand rules.
CREATE FUNCTION public.rules_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF NOT (session_user = 'migrator' AND OLD.tenant_id IS NOT NULL
            AND nullif(current_setting('app.purge_tenant', true), '')::uuid = OLD.tenant_id) THEN
      RAISE EXCEPTION 'rules are deleted only by the purge' USING ERRCODE = 'GRD01';
    END IF;
    RETURN OLD;
  END IF;
  IF (to_jsonb(NEW) - ARRAY['enabled', 'revision', 'updated_at'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['enabled', 'revision', 'updated_at']) THEN
    RAISE EXCEPTION 'only rules.enabled changes; new content is a new version row'
      USING ERRCODE = 'GRD01';
  END IF;
  NEW.revision := nextval('public.rules_revision_seq');
  RETURN NEW;
END
$$;

CREATE TRIGGER rules_guard BEFORE UPDATE OR DELETE ON public.rules
  FOR EACH ROW EXECUTE FUNCTION public.rules_guard();

CREATE FUNCTION public.category_templates_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'category templates are never deleted' USING ERRCODE = 'GRD01';
  END IF;
  IF (to_jsonb(NEW) - 'is_current') IS DISTINCT FROM (to_jsonb(OLD) - 'is_current') THEN
    RAISE EXCEPTION 'a template''s content is immutable; only is_current moves'
      USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER category_templates_guard BEFORE UPDATE OR DELETE ON public.category_templates
  FOR EACH ROW EXECUTE FUNCTION public.category_templates_guard();

CREATE FUNCTION public.error_map_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'error map rows are never deleted (disable them)' USING ERRCODE = 'GRD01';
  END IF;
  IF (to_jsonb(NEW) - 'enabled') IS DISTINCT FROM (to_jsonb(OLD) - 'enabled') THEN
    RAISE EXCEPTION 'only marketplace_error_map.enabled changes' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER error_map_guard BEFORE UPDATE OR DELETE ON public.marketplace_error_map
  FOR EACH ROW EXECUTE FUNCTION public.error_map_guard();

CREATE FUNCTION public.dictionary_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '% rows are never deleted (disable them)', TG_TABLE_NAME
      USING ERRCODE = 'GRD01';
  END IF;
  IF (to_jsonb(NEW) - 'revision') IS DISTINCT FROM (to_jsonb(OLD) - 'revision') THEN
    NEW.revision := nextval('public.rules_revision_seq');
  ELSE
    NEW.revision := OLD.revision;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER dictionary_guard BEFORE UPDATE OR DELETE ON public.attribute_definitions
  FOR EACH ROW EXECUTE FUNCTION public.dictionary_guard();
CREATE TRIGGER dictionary_guard BEFORE UPDATE OR DELETE ON public.category_attributes
  FOR EACH ROW EXECUTE FUNCTION public.dictionary_guard();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.category_templates, public.marketplace_error_map,
  public.attribute_definitions, public.attribute_columns, public.category_attributes FROM app_rw;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.rules FROM app_rw;
GRANT INSERT (tenant_id, marketplace, category, field, rule_type, subject, pack, severity, version,
              enabled, rule_def) ON public.rules TO app_rw;
GRANT UPDATE (enabled) ON public.rules TO app_rw;
GRANT USAGE ON SEQUENCE public.rules_revision_seq TO app_rw;
