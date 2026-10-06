-- 0004/13_variant_guard.up.sql
-- DATABASE.md §5.2 products "Variant guard" (:418) and the variant_key index (:423). For a child
-- (parent_product_id set), on INSERT, on restore (deleted_at back to NULL) and whenever its
-- parent, category or attributes change:
--   * the parent is a live style (a theme, not soft-deleted) in the same category, read FOR
--     SHARE (a concurrent soft-delete or category change waits), and is not itself a child (one
--     level deep);
--   * the variant_attributes keys are exactly the theme's axes (size; color; size and color);
--   * the guard writes variant_key: keys sorted, values trimmed and lowercased
--     (`color=jet black|size=m`), so `M/Navy` and `m/navy` collide on
--     uq_products_tenant_id_parent_product_id_variant_key (23505).
-- A style with live children cannot be soft-deleted, become a child, or change its category or
-- variation_theme (a concurrent child insert holds the style FOR SHARE, so the two serialise).
-- Refusals → GRD01.
CREATE FUNCTION public.variant_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  parent record;
  axes text[];
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
     AND EXISTS (SELECT 1 FROM public.products c
                  WHERE c.tenant_id = NEW.tenant_id AND c.parent_product_id = NEW.id
                    AND c.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'a style with live children cannot be deleted' USING ERRCODE = 'GRD01';
  END IF;
  -- the style side of the same rules: a style with live children stays a top-level style in its
  -- category with its theme (the children's axes and category are checked against both)
  IF TG_OP = 'UPDATE'
     AND (NEW.parent_product_id IS NOT NULL OR NEW.category IS DISTINCT FROM OLD.category
          OR NEW.variation_theme IS DISTINCT FROM OLD.variation_theme)
     AND EXISTS (SELECT 1 FROM public.products c
                  WHERE c.tenant_id = OLD.tenant_id AND c.parent_product_id = OLD.id
                    AND c.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'a style with live children keeps its category and theme and has no parent'
      USING ERRCODE = 'GRD01';
  END IF;
  IF NEW.parent_product_id IS NULL THEN
    NEW.variant_key := NULL;
    RETURN NEW;
  END IF;
  -- the key is always the guard's, never the writer's
  NEW.variant_key := (SELECT string_agg(e.key || '=' || lower(btrim(e.value)), '|' ORDER BY e.key)
                        FROM jsonb_each_text(NEW.variant_attributes) e);
  IF TG_OP = 'UPDATE' AND NEW.parent_product_id IS NOT DISTINCT FROM OLD.parent_product_id
     AND NEW.category IS NOT DISTINCT FROM OLD.category
     AND NEW.variant_attributes IS NOT DISTINCT FROM OLD.variant_attributes
     -- restoring a soft-deleted child re-checks its parent: the style-side freeze above counts
     -- only live children, so the style may have changed while the child was deleted
     AND NOT (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL) THEN
    RETURN NEW;
  END IF;

  SELECT p.category, p.variation_theme, p.parent_product_id, p.deleted_at INTO parent
    FROM public.products p
   WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.parent_product_id FOR SHARE;
  IF NOT FOUND THEN
    RETURN NEW;  -- the composite parent FK refuses
  END IF;
  IF parent.parent_product_id IS NOT NULL THEN
    RAISE EXCEPTION 'a variant is one level deep' USING ERRCODE = 'GRD01';
  END IF;
  IF parent.variation_theme IS NULL OR parent.deleted_at IS NOT NULL THEN
    RAISE EXCEPTION 'a variant''s parent is a live style with a theme' USING ERRCODE = 'GRD01';
  END IF;
  IF parent.category <> NEW.category THEN
    RAISE EXCEPTION 'a variant is in its style''s category' USING ERRCODE = 'GRD01';
  END IF;
  axes := CASE parent.variation_theme WHEN 'size' THEN ARRAY['size'] WHEN 'color' THEN ARRAY['color']
                                      ELSE ARRAY['color', 'size'] END;
  IF ARRAY(SELECT k FROM jsonb_object_keys(NEW.variant_attributes) AS k ORDER BY k) <> axes
     OR EXISTS (SELECT 1 FROM jsonb_each(NEW.variant_attributes) e
                 WHERE jsonb_typeof(e.value) <> 'string' OR btrim(e.value #>> '{}') = '') THEN
    RAISE EXCEPTION 'variant_attributes must hold exactly the theme''s axes (%)', axes
      USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER variant_guard BEFORE INSERT OR UPDATE ON public.products
  FOR EACH ROW EXECUTE FUNCTION public.variant_guard();
