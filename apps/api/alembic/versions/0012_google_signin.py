"""google_signin: Sign in with Google, always followed by Kuinn's own login code

ADR-0020 (proposed; amends RULES §4 and ADR-0005 for RS256 on Google ID tokens only). Ledger row
claimed in CANON.md with the Tech Lead's OK on 2026-10-30. Expand only, as migrator, in one
transaction plus a trailing autocommit block (the CHECKs added NOT VALID to existing tables are
validated there, and the users index is built CONCURRENTLY, the 0009 pattern):

1. `users.google_sub text` (nullable; CHECK 1..255 characters) and the partial unique index
   `uq_users_google_sub` (one Google account per user and one user per Google account). Not
   granted to the read-only roles: it is an identifier.
2. `signup_requests.verified_via text` (NULL or `google`; the "Verified by Google" badge of the
   admin queue), read by the read-only roles, set only at insert by the definer below.
3. `mfa_challenges.link_google_sub text`: a Google account waiting to be linked to an existing
   user, stored only on a challenge that proved the user's password (CHECK), and linked only
   when that sign-in finishes. `ck_mfa_challenges_amr` is widened to accept `fed` (a sign-in
   whose first factor was Google): the CHECK is dropped and re-added NOT VALID, then validated.
   `refresh_tokens.amr` is unchanged: `fed` never reaches a token.
4. `login_flows`: a platform table (no `tenant_id`; a named invariant-5 exception, like
   `signup_requests`) holding the state of one Google sign-in between the browser's redirects:
   phase `authorize` (the PKCE verifier and the nonce's sha256, 10 minutes), `handle` (the
   verified identity, 60 seconds), `details` (a new user's sign-up form, 15 minutes) and `link`
   (an existing user's password step, 15 minutes). Each row is found only by the sha256 of its
   opaque value (`flow_hash`) and is bound to the browser by `binding_hash`. ENABLE + FORCE RLS:
   app_rw reads and deletes only the row named by `app.token_hash` (`lf_hash_select`,
   `lf_hash_delete`) and inserts rows that expire within 15 minutes (`lf_insert`); no UPDATE or
   TRUNCATE. The read-only roles read `id, phase, expires_at, created_at` only.
5. `users_credential_guard` (BEFORE INSERT OR UPDATE OF `google_sub`, `auth_provider`,
   `email_verified_at`): GRD01 unless the current role is migrator (the definers below, the
   erasure functions, the staff CLI) or a superuser. `hashed_password` is deliberately not
   covered: the DPP-1 password changes write it as app_rw.
   `signup_requests_verified_via_guard`: GRD01 for an insert with `verified_via` set by anyone
   but migrator or a superuser, and for any later change of it.
6. SECURITY DEFINER functions (owner migrator, pinned search_path, EXECUTE for app_rw only):
   `app_google_user(sub)` (the live user linked to a Google account), `app_create_google_applicant`
   (a new Google user, no password, `auth_provider 'google'`, `email_verified_at now()`, with a
   pending `signup_requests` row `verified_via 'google'`; NULL when the email or the Google
   account is taken; only for `app.login_email`), `app_link_google_sub(user, sub)` (only for
   `app.user_id`, a live user with a password who is not a platform admin and has no Google
   account yet), `app_unlink_google_sub(user)` (only for `app.user_id`, only while the user has a
   password) and `app_prune_login_flows()` (deletes expired rows; the daily maintenance job).
7. `redact_user` and `purge_tenant` redefined (`sql/0012/01_erasure.up.sql`) to clear
   `google_sub` and delete the erased user's `login_flows` rows.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores 0011's erasure bodies, then
undoes the rest in reverse (challenges holding `fed` are deleted before the narrower CHECK).

Revision ID: 0012_google_signin
Revises: 0011b_account_purge
Create Date: 2026-10-30

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_google_signin"
down_revision: str | Sequence[str] | None = "0011b_account_purge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER: Final = "(SELECT public.app_user_id())"
HASH_GUC: Final = "(SELECT public.app_token_hash())"
READ_ONLY_ROLES: Final = "app_ro, app_platform_ro"
SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0012"
STAFF_OR_SUPERUSER: Final = (
    "current_user = 'migrator' OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r "
    "WHERE r.rolname = current_user), false)"
)
# The longest a login_flows row may live (the details and link phases); lf_insert enforces it.
FLOW_TTL_MAX: Final = "interval '15 minutes'"

COLUMNS_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.users ADD COLUMN google_sub text",
    "ALTER TABLE public.users ADD CONSTRAINT ck_users_google_sub "
    "CHECK (char_length(google_sub) BETWEEN 1 AND 255) NOT VALID",
    "ALTER TABLE public.signup_requests ADD COLUMN verified_via text",
    "ALTER TABLE public.signup_requests ADD CONSTRAINT ck_signup_requests_verified_via "
    "CHECK (verified_via IS NULL OR verified_via = 'google') NOT VALID",
    f"GRANT SELECT (verified_via) ON public.signup_requests TO {READ_ONLY_ROLES}",
    "ALTER TABLE public.mfa_challenges ADD COLUMN link_google_sub text",
    "ALTER TABLE public.mfa_challenges ADD CONSTRAINT ck_mfa_challenges_link_google_sub "
    "CHECK (link_google_sub IS NULL OR (char_length(link_google_sub) BETWEEN 1 AND 255 "
    "AND 'pwd' = ANY (amr))) NOT VALID",
    "ALTER TABLE public.mfa_challenges DROP CONSTRAINT ck_mfa_challenges_amr",
    "ALTER TABLE public.mfa_challenges ADD CONSTRAINT ck_mfa_challenges_amr "
    "CHECK (amr <@ ARRAY['pwd', 'otp', 'rc', 'fed']::text[] AND cardinality(amr) BETWEEN 1 AND 3) "
    "NOT VALID",
)
COLUMNS_DOWN: Final[tuple[str, ...]] = (
    "DELETE FROM public.mfa_challenges WHERE 'fed' = ANY (amr) OR link_google_sub IS NOT NULL",
    "ALTER TABLE public.mfa_challenges DROP CONSTRAINT ck_mfa_challenges_amr",
    "ALTER TABLE public.mfa_challenges ADD CONSTRAINT ck_mfa_challenges_amr "
    "CHECK (amr <@ ARRAY['pwd', 'otp', 'rc']::text[] AND cardinality(amr) BETWEEN 1 AND 3)",
    "ALTER TABLE public.mfa_challenges DROP CONSTRAINT ck_mfa_challenges_link_google_sub",
    "ALTER TABLE public.mfa_challenges DROP COLUMN link_google_sub",
    "ALTER TABLE public.signup_requests DROP CONSTRAINT ck_signup_requests_verified_via",
    "ALTER TABLE public.signup_requests DROP COLUMN verified_via",
    "ALTER TABLE public.users DROP CONSTRAINT ck_users_google_sub",
    "ALTER TABLE public.users DROP COLUMN google_sub",
)

TABLE_UP: Final[tuple[str, ...]] = (
    """
    CREATE TABLE public.login_flows (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      phase text NOT NULL,
      flow_hash bytea NOT NULL,
      binding_hash bytea NOT NULL,
      code_verifier text,
      nonce_hash bytea,
      sub text,
      email text,
      hd text,
      display_name text,
      given_name text,
      family_name text,
      expires_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_login_flows PRIMARY KEY (id),
      CONSTRAINT uq_login_flows_flow_hash UNIQUE (flow_hash),
      CONSTRAINT ck_login_flows_phase
        CHECK (phase IN ('authorize', 'handle', 'details', 'link')),
      CONSTRAINT ck_login_flows_hashes
        CHECK (octet_length(flow_hash) = 32 AND octet_length(binding_hash) = 32
               AND (nonce_hash IS NULL OR octet_length(nonce_hash) = 32)),
      CONSTRAINT ck_login_flows_phase_columns CHECK (
        (phase = 'authorize') = (code_verifier IS NOT NULL)
        AND (phase = 'authorize') = (nonce_hash IS NOT NULL)
        AND (phase = 'authorize') = (sub IS NULL)
        AND (sub IS NULL) = (email IS NULL)
      ),
      CONSTRAINT ck_login_flows_bounds CHECK (
        char_length(code_verifier) BETWEEN 43 AND 128
        AND char_length(sub) BETWEEN 1 AND 255
        AND octet_length(email) <= 254
        AND char_length(hd) <= 253
        AND char_length(display_name) <= 200
        AND char_length(given_name) <= 200
        AND char_length(family_name) <= 200
      )
    )
    """,
    "CREATE INDEX ix_login_flows_expires_at ON public.login_flows (expires_at)",
    "REVOKE UPDATE, TRUNCATE ON public.login_flows FROM app_rw",
    f"GRANT SELECT (id, phase, expires_at, created_at) ON public.login_flows TO {READ_ONLY_ROLES}",
    "ALTER TABLE public.login_flows ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.login_flows FORCE ROW LEVEL SECURITY",
)
TABLE_DOWN: Final[tuple[str, ...]] = ("DROP TABLE public.login_flows",)

# (policy name, statement), all on login_flows
POLICIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "lf_hash_select",
        "CREATE POLICY lf_hash_select ON public.login_flows FOR SELECT TO app_rw "
        f"USING (flow_hash = {HASH_GUC})",
    ),
    (
        "lf_hash_delete",
        "CREATE POLICY lf_hash_delete ON public.login_flows FOR DELETE TO app_rw "
        f"USING (flow_hash = {HASH_GUC})",
    ),
    (
        "lf_insert",
        "CREATE POLICY lf_insert ON public.login_flows FOR INSERT TO app_rw "
        f"WITH CHECK (expires_at > now() AND expires_at <= now() + {FLOW_TTL_MAX})",
    ),
    (
        "t_platform",
        f"CREATE POLICY t_platform ON public.login_flows FOR SELECT TO {READ_ONLY_ROLES} "
        "USING (current_user IN ('app_platform_ro', 'app_ro'))",
    ),
    (
        "t_migrator",
        "CREATE POLICY t_migrator ON public.login_flows TO migrator USING (true) WITH CHECK (true)",
    ),
)

GUARDS_UP: Final[tuple[str, ...]] = (
    f"""
    CREATE FUNCTION public.users_credential_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF (TG_OP = 'INSERT' AND (NEW.google_sub IS NOT NULL OR NEW.auth_provider IS NOT NULL
                                OR NEW.email_verified_at IS NOT NULL))
         OR (TG_OP = 'UPDATE'
             AND (NEW.google_sub IS DISTINCT FROM OLD.google_sub
                  OR NEW.auth_provider IS DISTINCT FROM OLD.auth_provider
                  OR NEW.email_verified_at IS DISTINCT FROM OLD.email_verified_at)) THEN
        IF NOT ({STAFF_OR_SUPERUSER}) THEN
          RAISE EXCEPTION 'users Google and verification columns move only through their functions'
            USING ERRCODE = 'GRD01';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER users_credential_guard BEFORE INSERT OR UPDATE OF google_sub, auth_provider, "
    "email_verified_at ON public.users FOR EACH ROW "
    "EXECUTE FUNCTION public.users_credential_guard()",
    f"""
    CREATE FUNCTION public.signup_requests_verified_via_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW.verified_via IS DISTINCT FROM OLD.verified_via THEN
        RAISE EXCEPTION 'signup_requests.verified_via never changes' USING ERRCODE = 'GRD01';
      END IF;
      IF TG_OP = 'INSERT' AND NEW.verified_via IS NOT NULL AND NOT ({STAFF_OR_SUPERUSER}) THEN
        RAISE EXCEPTION 'signup_requests.verified_via is set only by its function'
          USING ERRCODE = 'GRD01';
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER signup_requests_verified_via_guard BEFORE INSERT OR UPDATE OF verified_via "
    "ON public.signup_requests FOR EACH ROW "
    "EXECUTE FUNCTION public.signup_requests_verified_via_guard()",
)
GUARDS_DOWN: Final[tuple[str, ...]] = (
    "DROP TRIGGER signup_requests_verified_via_guard ON public.signup_requests",
    "DROP FUNCTION public.signup_requests_verified_via_guard()",
    "DROP TRIGGER users_credential_guard ON public.users",
    "DROP FUNCTION public.users_credential_guard()",
)

DEFINERS: Final[tuple[str, ...]] = (
    "public.app_google_user(text)",
    "public.app_create_google_applicant(text, text, text, text, text, text, text)",
    "public.app_link_google_sub(uuid, text)",
    "public.app_unlink_google_sub(uuid)",
    "public.app_prune_login_flows()",
)
FUNCTIONS_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.app_google_user(p_sub text) RETURNS uuid
      LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
      SELECT u.id FROM public.users u WHERE u.google_sub = p_sub AND u.erased_at IS NULL
    $$
    """,
    """
    CREATE FUNCTION public.app_create_google_applicant(
      p_email text, p_sub text, p_display_name text, p_brand_name text, p_owner_name text,
      p_phone text, p_website text
    ) RETURNS uuid
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      new_user uuid := gen_random_uuid();
      new_request uuid := gen_random_uuid();
    BEGIN
      IF p_email IS NULL OR lower(p_email) IS DISTINCT FROM lower(public.app_login_email()) THEN
        RAISE EXCEPTION 'a Google applicant is created only for app.login_email'
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
      INSERT INTO public.signup_requests (id, user_id, brand_name, owner_name, phone, website,
                                          verified_via)
      VALUES (new_request, new_user, p_brand_name, p_owner_name, p_phone, p_website, 'google');
      RETURN new_request;
    END
    $$
    """,
    """
    CREATE FUNCTION public.app_link_google_sub(p_user uuid, p_sub text) RETURNS boolean
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
    $$
    """,
    """
    CREATE FUNCTION public.app_unlink_google_sub(p_user uuid) RETURNS boolean
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      unlinked boolean;
    BEGIN
      UPDATE public.users u SET google_sub = NULL
       WHERE u.id = p_user AND p_user = public.app_user_id()
         AND u.google_sub IS NOT NULL AND u.hashed_password IS NOT NULL
      RETURNING true INTO unlinked;
      RETURN coalesce(unlinked, false);
    END
    $$
    """,
    """
    CREATE FUNCTION public.app_prune_login_flows() RETURNS integer
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      n integer;
    BEGIN
      DELETE FROM public.login_flows f WHERE f.expires_at <= now();
      GET DIAGNOSTICS n = ROW_COUNT;
      RETURN n;
    END
    $$
    """,
    f"REVOKE ALL ON FUNCTION {', '.join(DEFINERS)} FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {', '.join(DEFINERS)} TO app_rw",
)
FUNCTIONS_DOWN: Final[tuple[str, ...]] = tuple(f"DROP FUNCTION {name}" for name in DEFINERS)

# Validated after the main transaction commits (the 0009 and 0011 pattern), then the users index
# is built CONCURRENTLY (a rerun after an interrupted build replaces an invalid index).
VALIDATE: Final[tuple[str, ...]] = (
    "ALTER TABLE public.users VALIDATE CONSTRAINT ck_users_google_sub",
    "ALTER TABLE public.signup_requests VALIDATE CONSTRAINT ck_signup_requests_verified_via",
    "ALTER TABLE public.mfa_challenges VALIDATE CONSTRAINT ck_mfa_challenges_link_google_sub",
    "ALTER TABLE public.mfa_challenges VALIDATE CONSTRAINT ck_mfa_challenges_amr",
)
INDEX_NAME: Final = "uq_users_google_sub"
INDEX: Final = (
    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON public.users (google_sub) "
    "WHERE google_sub IS NOT NULL"
)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004 and 0011), so `text()` never reads one as a bind parameter.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in (*COLUMNS_UP, *TABLE_UP):
        op.execute(statement)
    for _, statement in POLICIES:
        op.execute(statement)
    for statement in (*GUARDS_UP, *FUNCTIONS_UP):
        op.execute(statement)
    _run_sql("01_erasure.up.sql")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for statement in VALIDATE:
            op.execute(statement)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX_NAME}")
        op.execute(INDEX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX_NAME}")
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_erasure.down.sql")
    for statement in (*FUNCTIONS_DOWN, *GUARDS_DOWN):
        op.execute(statement)
    for name, _ in reversed(POLICIES):
        op.execute(f"DROP POLICY {name} ON public.login_flows")
    for statement in (*TABLE_DOWN, *COLUMNS_DOWN):
        op.execute(statement)
