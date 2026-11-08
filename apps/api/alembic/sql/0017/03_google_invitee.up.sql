-- 0017/03_google_invitee.up.sql
-- SI5 (ADR-0029 §2; CP-D1): an invite accepted with Google creates a password-less user with the
-- Google `sub`. users_credential_guard (0012) refuses that insert for app_rw, so it runs here, as
-- 0012's app_create_google_applicant does, without the sign-up request row. Only for
-- `app.login_email`, and only while a pending, unexpired invitation for that email is named by
-- `app.token_hash` (locked FOR UPDATE until the accept commits). NULL when the email or the
-- Google account is taken. Owner migrator, pinned search_path, EXECUTE for app_rw only.
CREATE FUNCTION public.app_create_google_invitee(p_email text, p_sub text, p_display_name text)
  RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  new_user uuid := gen_random_uuid();
BEGIN
  IF p_email IS NULL OR lower(p_email) IS DISTINCT FROM lower(public.app_login_email()) THEN
    RAISE EXCEPTION 'a Google invitee is created only for app.login_email' USING ERRCODE = 'GRD01';
  END IF;
  PERFORM 1 FROM public.invitations i
   WHERE i.token_hash = public.app_token_hash() AND i.email = lower(p_email)
     AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now()
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'a Google invitee needs a pending invitation for the email'
      USING ERRCODE = 'GRD01';
  END IF;
  IF EXISTS (SELECT 1 FROM public.users u WHERE lower(u.email) = lower(p_email))
     OR EXISTS (SELECT 1 FROM public.users u WHERE u.google_sub = p_sub) THEN
    RETURN NULL;
  END IF;
  BEGIN
    INSERT INTO public.users (id, email, hashed_password, auth_provider, display_name,
                              email_verified_at, google_sub)
    VALUES (new_user, lower(p_email), NULL, 'google', p_display_name, now(), p_sub);
  EXCEPTION WHEN unique_violation THEN
    RETURN NULL;
  END;
  RETURN new_user;
END
$$;

REVOKE ALL ON FUNCTION public.app_create_google_invitee(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.app_create_google_invitee(text, text, text) TO app_rw;
