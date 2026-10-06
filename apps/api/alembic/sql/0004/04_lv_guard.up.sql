-- 0004/04_lv_guard.up.sql
-- DATABASE.md §5.3 listing_versions "lv_guard" and "Privileges" (:786-789); BACKEND-ARCHITECTURE
-- §9.3 (BA:2065-2071). A version is immutable except its one-time approval:
--   * any other column changing, or an approval changing once set → GRD01;
--   * the approver needs an active `reviewer` or `owner` membership, read FOR SHARE (a concurrent
--     deactivation waits, or is seen); an approver with no membership in this tenant is left to
--     the membership FK (23503);
--   * not the generator or the editor, the version must be the listing's current one, and nobody
--     who generated or edited a version back to the last `generation` or full `regeneration`
--     (`changed_paths` NULL) may approve → MKC01 (→ 403 MAKER_CHECKER_VIOLATION);
--   * `approved_at` is stamped here.
-- app_rw may UPDATE only the two approval columns and may not DELETE.
CREATE FUNCTION public.lv_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  approver uuid := NEW.approved_by_user_id;
  m record;
  v record;
  current_id uuid;
  walk uuid;
  seen uuid[] := ARRAY[NEW.id];
BEGIN
  IF (to_jsonb(NEW) - 'approved_by_user_id' - 'approved_at')
     IS DISTINCT FROM (to_jsonb(OLD) - 'approved_by_user_id' - 'approved_at') THEN
    RAISE EXCEPTION 'listing_versions are immutable except the approval' USING ERRCODE = 'GRD01';
  END IF;
  IF approver IS NOT DISTINCT FROM OLD.approved_by_user_id THEN
    IF NEW.approved_at IS DISTINCT FROM OLD.approved_at THEN
      RAISE EXCEPTION 'the approval is set once' USING ERRCODE = 'GRD01';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.approved_by_user_id IS NOT NULL OR approver IS NULL THEN
    RAISE EXCEPTION 'the approval is set once' USING ERRCODE = 'GRD01';
  END IF;
  NEW.approved_at := now();

  SELECT tm.role, tm.is_active INTO m FROM public.tenant_memberships tm
   WHERE tm.tenant_id = NEW.tenant_id AND tm.user_id = approver FOR SHARE;
  IF NOT FOUND THEN
    RETURN NEW;  -- not a member of this tenant: fk_listing_versions_approved_by_user_id refuses
  END IF;
  IF NOT m.is_active OR m.role NOT IN ('reviewer', 'owner') THEN
    RAISE EXCEPTION 'approver needs an active reviewer or owner membership' USING ERRCODE = 'MKC01';
  END IF;
  IF approver = NEW.generated_by_user_id OR approver = NEW.edited_by_user_id THEN
    RAISE EXCEPTION 'the maker or editor cannot approve' USING ERRCODE = 'MKC01';
  END IF;
  SELECT l.current_version_id INTO current_id FROM public.listings l
   WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.listing_id;
  IF current_id IS DISTINCT FROM NEW.id THEN
    RAISE EXCEPTION 'only the current version can be approved' USING ERRCODE = 'MKC01';
  END IF;

  -- contributor chain: walk parents until the last generation or full regeneration
  IF NOT (NEW.origin = 'generation' OR (NEW.origin = 'regeneration' AND NEW.changed_paths IS NULL))
  THEN
    walk := NEW.parent_version_id;
    WHILE walk IS NOT NULL AND NOT walk = ANY (seen) LOOP
      seen := seen || walk;
      SELECT lv.generated_by_user_id, lv.edited_by_user_id, lv.origin, lv.changed_paths,
             lv.parent_version_id
        INTO v FROM public.listing_versions lv
       WHERE lv.listing_id = NEW.listing_id AND lv.id = walk;
      EXIT WHEN NOT FOUND;
      IF approver = v.generated_by_user_id OR approver = v.edited_by_user_id THEN
        RAISE EXCEPTION 'an earlier contributor cannot approve' USING ERRCODE = 'MKC01';
      END IF;
      EXIT WHEN v.origin = 'generation' OR (v.origin = 'regeneration' AND v.changed_paths IS NULL);
      walk := v.parent_version_id;
    END LOOP;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER lv_guard BEFORE UPDATE ON public.listing_versions
  FOR EACH ROW EXECUTE FUNCTION public.lv_guard();

REVOKE UPDATE, DELETE, TRUNCATE ON public.listing_versions FROM app_rw;
GRANT UPDATE (approved_by_user_id, approved_at) ON public.listing_versions TO app_rw;
