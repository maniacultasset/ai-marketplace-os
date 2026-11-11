-- 0029/01_link_google_sub.down.sql (local and CI only): refuses while a platform admin holds a
-- linked Google account (the 0012 state never has one), then restores 0012's body, which refuses
-- to link a platform admin.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.users WHERE is_platform_admin AND google_sub IS NOT NULL) THEN
    RAISE EXCEPTION '0029 downgrade refused: a platform admin has a linked Google account'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
CREATE OR REPLACE FUNCTION public.app_link_google_sub(p_user uuid, p_sub text) RETURNS boolean
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  linked boolean;
BEGIN
  UPDATE public.users u SET google_sub = p_sub
   WHERE u.id = p_user AND p_user = public.app_user_id()
     AND u.google_sub IS NULL AND u.hashed_password IS NOT NULL
     AND u.is_active AND u.erased_at IS NULL AND NOT u.is_platform_admin
     AND NOT EXISTS (SELECT 1 FROM public.users o WHERE o.google_sub = p_sub)
  RETURNING true INTO linked;
  RETURN coalesce(linked, false);
EXCEPTION WHEN unique_violation THEN
  RETURN false;
END
$$;
