-- 0027/02_category_suggestion.up.sql (SI14): the classifier's stored suggestion (model text, read
-- like `description` through the read-only roles' table-level SELECT on products). The shape follows the seam's CategoryCandidate
-- (mkos_schemas.classify; pipeline contract 1.1.0): at most 3 candidates, rationale <= 300,
-- evidence_refs <= 12 of <= 200 characters. Nested IFs only: an IF condition that is NULL is not
-- taken, and plpgsql may evaluate both operands of OR.
ALTER TABLE public.products ADD COLUMN category_suggestion jsonb;

CREATE FUNCTION public.category_suggestion_valid(p jsonb) RETURNS boolean
  LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  c jsonb;
  e jsonb;
BEGIN
  IF p IS NULL OR jsonb_typeof(p) IS DISTINCT FROM 'object' THEN
    RETURN false;
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_object_keys(p) k
              WHERE k NOT IN ('status', 'candidates', 'input_digest', 'calibration_version',
                              'job_id', 'at')) THEN
    RETURN false;
  END IF;
  IF jsonb_typeof(p -> 'status') IS DISTINCT FROM 'string' THEN
    RETURN false;
  END IF;
  IF (p ->> 'status') NOT IN ('suggested', 'unavailable', 'failed', 'confirmed', 'overridden') THEN
    RETURN false;
  END IF;
  IF jsonb_typeof(p -> 'candidates') IS DISTINCT FROM 'array' THEN
    RETURN false;
  END IF;
  IF jsonb_array_length(p -> 'candidates') > 3 THEN
    RETURN false;
  END IF;
  FOR c IN SELECT x.value FROM jsonb_array_elements(p -> 'candidates') x LOOP
    IF jsonb_typeof(c) IS DISTINCT FROM 'object' THEN
      RETURN false;
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(c) k
                WHERE k NOT IN ('category', 'confidence', 'rationale', 'evidence_refs')) THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(c -> 'category') IS DISTINCT FROM 'string' THEN
      RETURN false;
    END IF;
    IF NOT coalesce((c ->> 'category') ~ '^[a-z0-9_]{1,64}$', false) THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(c -> 'confidence') IS DISTINCT FROM 'number' THEN
      RETURN false;
    END IF;
    IF NOT coalesce((c ->> 'confidence')::numeric BETWEEN 0 AND 1, false) THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(c -> 'rationale') IS DISTINCT FROM 'string' THEN
      RETURN false;
    END IF;
    IF char_length(c ->> 'rationale') > 300 THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(c -> 'evidence_refs') IS DISTINCT FROM 'array' THEN
      RETURN false;
    END IF;
    IF jsonb_array_length(c -> 'evidence_refs') > 12 THEN
      RETURN false;
    END IF;
    FOR e IN SELECT y.value FROM jsonb_array_elements(c -> 'evidence_refs') y LOOP
      IF jsonb_typeof(e) IS DISTINCT FROM 'string' THEN
        RETURN false;
      END IF;
      IF char_length(e #>> '{}') NOT BETWEEN 1 AND 200 THEN
        RETURN false;
      END IF;
    END LOOP;
  END LOOP;
  IF jsonb_typeof(p -> 'input_digest') IS NOT NULL AND jsonb_typeof(p -> 'input_digest') <> 'null' THEN
    IF jsonb_typeof(p -> 'input_digest') <> 'string' THEN
      RETURN false;
    END IF;
    IF NOT coalesce((p ->> 'input_digest') ~ '^[0-9a-f]{64}$', false) THEN
      RETURN false;
    END IF;
  END IF;
  IF jsonb_typeof(p -> 'calibration_version') IS NOT NULL
     AND jsonb_typeof(p -> 'calibration_version') <> 'null' THEN
    IF jsonb_typeof(p -> 'calibration_version') <> 'string' THEN
      RETURN false;
    END IF;
    IF char_length(p ->> 'calibration_version') NOT BETWEEN 1 AND 32 THEN
      RETURN false;
    END IF;
  END IF;
  IF jsonb_typeof(p -> 'job_id') IS NOT NULL AND jsonb_typeof(p -> 'job_id') <> 'null' THEN
    IF jsonb_typeof(p -> 'job_id') <> 'string' THEN
      RETURN false;
    END IF;
    IF NOT coalesce((p ->> 'job_id')
         ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) THEN
      RETURN false;
    END IF;
  END IF;
  IF jsonb_typeof(p -> 'at') IS DISTINCT FROM 'string' THEN
    RETURN false;
  END IF;
  IF char_length(p ->> 'at') NOT BETWEEN 1 AND 40 THEN
    RETURN false;
  END IF;
  RETURN true;
END
$$;
-- evaluated by the CHECK as the writing role (SECURITY INVOKER)
GRANT EXECUTE ON FUNCTION public.category_suggestion_valid(jsonb) TO app_rw;

ALTER TABLE public.products ADD CONSTRAINT ck_products_category_suggestion
  CHECK (category_suggestion IS NULL OR public.category_suggestion_valid(category_suggestion))
  NOT VALID;
ALTER TABLE public.products VALIDATE CONSTRAINT ck_products_category_suggestion;
