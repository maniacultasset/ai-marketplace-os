-- 0029/01_link_google_sub.up.sql (ADR-0020 amendment, owner decision 11 Nov 2026): a platform
-- admin may link a Google account, like any live user with a password, because the admin's
-- Google sign-in always ends with the Kuinn code. The body is 0012's without
-- `AND NOT u.is_platform_admin`; CREATE OR REPLACE keeps the owner (migrator), the pinned
-- search_path and the EXECUTE grant (app_rw only).
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
     AND u.is_active AND u.erased_at IS NULL
     AND NOT EXISTS (SELECT 1 FROM public.users o WHERE o.google_sub = p_sub)
  RETURNING true INTO linked;
  RETURN coalesce(linked, false);
EXCEPTION WHEN unique_violation THEN
  RETURN false;
END
$$;
