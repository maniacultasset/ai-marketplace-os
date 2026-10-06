-- 0004/13b_matrix_guard.up.sql
-- DATABASE.md §5.2 variant_matrix_cells "variant_matrix_guard" (:452) and images (:480).
-- variant_matrix_guard, BEFORE INSERT OR UPDATE of a cell → VAR01:
--   * the style is live with a theme, read FOR SHARE;
--   * the axes follow the theme (size_color: size_key and colourway_id; size: size_key only;
--     color: colourway_id only) and size_key is in the style's size_run;
--   * an available cell's child has parent_product_id = style_product_id, variant_attributes =
--     jsonb_strip_nulls({size: size_key, color: colour_key}) and attributes.size.normalized =
--     size_key (when the cell has a size).
-- The matrix check, a DEFERRABLE INITIALLY DEFERRED constraint trigger → VAR02 at commit: every
-- live child is in exactly one available cell, and a soft-deleted child is in none (its cell was
-- flipped to unavailable/discontinued in the same transaction). It runs for the child's own
-- writes and for every cell write that touches a child.
-- The colour-image guard: a `size_chart` image only on a style or a standalone product → GRD01.
CREATE FUNCTION public.variant_matrix_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  s record;
  c record;
  colour text;
BEGIN
  SELECT p.variation_theme, p.size_run, p.deleted_at, p.parent_product_id INTO s
    FROM public.products p
   WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.style_product_id FOR SHARE;
  IF NOT FOUND THEN
    RETURN NEW;  -- the composite style FK refuses
  END IF;
  IF s.variation_theme IS NULL OR s.deleted_at IS NOT NULL OR s.parent_product_id IS NOT NULL THEN
    RAISE EXCEPTION 'a matrix cell belongs to a live style with a theme' USING ERRCODE = 'VAR01';
  END IF;
  IF (NEW.size_key IS NOT NULL) <> (s.variation_theme IN ('size', 'size_color'))
     OR (NEW.colourway_id IS NOT NULL) <> (s.variation_theme IN ('color', 'size_color')) THEN
    RAISE EXCEPTION 'the cell axes follow the theme %', s.variation_theme USING ERRCODE = 'VAR01';
  END IF;
  IF NEW.size_key IS NOT NULL AND NOT coalesce(NEW.size_key = ANY (s.size_run), false) THEN
    RAISE EXCEPTION 'size % is not in the style''s size_run', NEW.size_key USING ERRCODE = 'VAR01';
  END IF;
  IF NEW.availability = 'available' THEN
    IF NEW.colourway_id IS NOT NULL THEN
      SELECT cw.colour_key INTO colour FROM public.product_colourways cw
       WHERE cw.tenant_id = NEW.tenant_id AND cw.id = NEW.colourway_id;
    END IF;
    SELECT p.parent_product_id, p.variant_attributes, p.attributes INTO c
      FROM public.products p
     WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.variant_product_id FOR SHARE;
    IF NOT FOUND THEN
      RETURN NEW;  -- the composite variant FK refuses
    END IF;
    IF c.parent_product_id IS DISTINCT FROM NEW.style_product_id
       OR c.variant_attributes IS DISTINCT FROM
          jsonb_strip_nulls(jsonb_build_object('size', NEW.size_key, 'color', colour))
       OR (NEW.size_key IS NOT NULL
           AND (c.attributes #>> '{size,normalized}') IS DISTINCT FROM NEW.size_key) THEN
      RAISE EXCEPTION 'the available cell''s child does not match the cell' USING ERRCODE = 'VAR01';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER variant_matrix_guard BEFORE INSERT OR UPDATE ON public.variant_matrix_cells
  FOR EACH ROW EXECUTE FUNCTION public.variant_matrix_guard();

-- one check for a child id: live → exactly one available cell; soft-deleted → none
CREATE FUNCTION public.variant_matrix_check_child(p_tenant uuid, p_child uuid) RETURNS void
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  live boolean;
  cells int;
BEGIN
  SELECT p.deleted_at IS NULL INTO live FROM public.products p
   WHERE p.tenant_id = p_tenant AND p.id = p_child AND p.parent_product_id IS NOT NULL;
  IF NOT FOUND THEN
    RETURN;  -- gone (purge) or not a child
  END IF;
  SELECT count(*) INTO cells FROM public.variant_matrix_cells m
   WHERE m.tenant_id = p_tenant AND m.variant_product_id = p_child AND m.availability = 'available';
  IF live AND cells <> 1 THEN
    RAISE EXCEPTION 'live variant % is in % available cells, not 1', p_child, cells
      USING ERRCODE = 'VAR02';
  END IF;
  IF NOT live AND cells <> 0 THEN
    RAISE EXCEPTION 'soft-deleted variant % still has an available cell', p_child
      USING ERRCODE = 'VAR02';
  END IF;
END
$$;

-- called from the deferred trigger as the writing role
GRANT EXECUTE ON FUNCTION public.variant_matrix_check_child(uuid, uuid) TO app_rw;

CREATE FUNCTION public.variant_matrix_complete() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF TG_TABLE_NAME = 'products' THEN
    IF NEW.parent_product_id IS NOT NULL THEN
      PERFORM public.variant_matrix_check_child(NEW.tenant_id, NEW.id);
    END IF;
    RETURN NULL;
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.variant_product_id IS NOT NULL THEN
    PERFORM public.variant_matrix_check_child(OLD.tenant_id, OLD.variant_product_id);
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.variant_product_id IS NOT NULL THEN
    PERFORM public.variant_matrix_check_child(NEW.tenant_id, NEW.variant_product_id);
  END IF;
  RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER variant_matrix_complete AFTER INSERT OR UPDATE ON public.products
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.variant_matrix_complete();
CREATE CONSTRAINT TRIGGER variant_matrix_complete AFTER INSERT OR UPDATE OR DELETE
  ON public.variant_matrix_cells
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.variant_matrix_complete();

CREATE FUNCTION public.image_slot_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF NEW.slot = 'size_chart' AND EXISTS (
       SELECT 1 FROM public.products p
        WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.product_id
          AND p.parent_product_id IS NOT NULL) THEN
    RAISE EXCEPTION 'a size chart belongs to a style or a standalone product' USING ERRCODE = 'GRD01';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER image_slot_guard BEFORE INSERT OR UPDATE ON public.images
  FOR EACH ROW EXECUTE FUNCTION public.image_slot_guard();
