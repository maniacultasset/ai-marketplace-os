-- 0041/01_category_suggestion_flags.up.sql (client feedback 17 Nov): the stored suggestion may
-- carry `not_listed` (boolean: the model's best answer was "none of the offered categories") and
-- `looks_like` (null or a picker group: the seller's own words name another group than the top
-- candidate's). 0027's body otherwise unchanged; the CHECK keeps calling this function, so existing
-- rows stay valid (they carry neither key).
CREATE OR REPLACE FUNCTION public.category_suggestion_valid(p jsonb) RETURNS boolean
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
                              'job_id', 'at', 'not_listed', 'looks_like')) THEN
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
  IF jsonb_typeof(p -> 'not_listed') IS NOT NULL
     AND jsonb_typeof(p -> 'not_listed') <> 'boolean' THEN
    RETURN false;
  END IF;
  IF jsonb_typeof(p -> 'looks_like') IS NOT NULL AND jsonb_typeof(p -> 'looks_like') <> 'null' THEN
    IF jsonb_typeof(p -> 'looks_like') <> 'string' THEN
      RETURN false;
    END IF;
    IF (p ->> 'looks_like') NOT IN ('topwear', 'bottomwear', 'ethnic', 'home', 'other') THEN
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
