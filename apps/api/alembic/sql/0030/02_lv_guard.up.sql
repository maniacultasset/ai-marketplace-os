-- 0030/02_lv_guard.up.sql
-- ADR-0031 (owner decision 2026-11-11): the maker-checker backstop lets an approver in the
-- contributor chain through in exactly one case, as the API does (`maker_checker.approve_block`):
-- the approver's membership is an active `owner` (the role, active and email checks above still
-- run) and the tenant's `owner_self_approval` is on. It then sets `self_approved` to whether the
-- approver is in the chain; nobody else writes that column (INSERT refuses it true, and it is
-- part of the set-once approval). Every other check is 0026's body unchanged. SECURITY INVOKER:
-- app_rw reads its own tenant's row (RLS `t_tenant`).
CREATE OR REPLACE FUNCTION public.lv_guard() RETURNS trigger
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
  self_ok boolean;
  own boolean;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.approved_by_user_id IS NOT NULL OR NEW.approved_at IS NOT NULL OR NEW.self_approved THEN
      RAISE EXCEPTION 'a version is inserted unapproved; the approval is a later UPDATE'
        USING ERRCODE = 'GRD01';
    END IF;
    RETURN NEW;
  END IF;
  IF (to_jsonb(NEW) - 'approved_by_user_id' - 'approved_at' - 'self_approved')
     IS DISTINCT FROM (to_jsonb(OLD) - 'approved_by_user_id' - 'approved_at' - 'self_approved') THEN
    RAISE EXCEPTION 'listing_versions are immutable except the approval' USING ERRCODE = 'GRD01';
  END IF;
  IF approver IS NOT DISTINCT FROM OLD.approved_by_user_id THEN
    IF NEW.approved_at IS DISTINCT FROM OLD.approved_at
       OR NEW.self_approved IS DISTINCT FROM OLD.self_approved THEN
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
  -- 0026 (V1 hardening #12): an account made from a copied invite link, email unverified
  IF EXISTS (SELECT 1 FROM public.users u WHERE u.id = approver AND u.email_unverified) THEN
    RAISE EXCEPTION 'approver email is unverified' USING ERRCODE = 'MKC01';
  END IF;
  -- 0030 (ADR-0031): an active owner may approve its own work while the tenant's
  -- owner_self_approval is on; a reviewer never may, nor anyone while the setting is off
  self_ok := m.role = 'owner' AND coalesce(
    (SELECT t.owner_self_approval FROM public.tenants t WHERE t.id = NEW.tenant_id), false);
  own := approver = NEW.generated_by_user_id OR coalesce(approver = NEW.edited_by_user_id, false);
  IF own AND NOT self_ok THEN
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
        IF NOT self_ok THEN
          RAISE EXCEPTION 'an earlier contributor cannot approve' USING ERRCODE = 'MKC01';
        END IF;
        own := true;
      END IF;
      EXIT WHEN v.origin = 'generation' OR (v.origin = 'regeneration' AND v.changed_paths IS NULL);
      walk := v.parent_version_id;
    END LOOP;
  END IF;
  -- 0030: the approval says, for good, whether it was of the approver's own work
  NEW.self_approved := own;
  RETURN NEW;
END
$$;
