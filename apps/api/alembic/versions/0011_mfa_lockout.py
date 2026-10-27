"""mfa_lockout: TOTP factors, recovery codes, sign-in challenges, lockout and password policy

DPP-1 (Amazon DPP 1.2.2 and 1.4.1; ADR-0021, proposed; ledger claim and Tech Lead OK 2026-10-27).
Expand only, as migrator, in one transaction plus a trailing autocommit block that validates the
two CHECKs added NOT VALID to existing tables:

1. `users` gains `failed_auth_count` (0..10, constant default), `locked_until`,
   `mfa_last_changed_at`, `password_changed_at` (constant default `now()`: existing rows get the
   migration time with no rewrite) and `password_must_change`. The CHECK is added NOT VALID and
   validated after the main transaction commits (SHARE UPDATE EXCLUSIVE, not the ACCESS EXCLUSIVE
   scan of an inline CHECK; the 0009 pattern). The
   read-only roles read the five columns.
2. `users_auth_state_guard`: GRD01 when anyone but migrator or a superuser inserts a non-default
   counter or lock, or changes either. Conditional like `users_platform_admin_guard`, so the
   public sign-up insert (`users_signup`) still passes.
3. Three SECURITY DEFINER functions (owner migrator, pinned search_path, EXECUTE for app_rw only):
   `app_begin_auth_attempt(u)` takes the user row FOR UPDATE (5 s lock_timeout) and returns its
   counter and lock, serialising one user's sign-in attempts until the caller commits;
   `app_note_auth_failure(u)` counts one failure and at the 10th locks the account for 900
   seconds and resets the count, a no-op while locked; `app_clear_auth_failures(u)` resets both,
   only for `app_user_id()`.
4. `app_challenge_hash()`: `app.challenge_hash` as bytea, like `app_token_hash()`.
5. `user_mfa_factors` (one active and one pending TOTP factor per user; the secret is one sealed
   envelope blob) with `user_mfa_factors_guard`: app_rw inserts only pending factors and never
   rewrites the secret, its key version, owner or kind, and `last_used_step` never decreases.
6. `user_recovery_codes`: sha256 hashes only.
7. `mfa_challenges`: the opaque second-step token by its sha256, purpose, the methods proven so
   far, 5 attempts, expiry, and when a code was proven.
8. `user_password_history`: the previous hashes (the current one plus 9 = the last 10); app_rw
   cannot update them.
9. `refresh_tokens` gains `amr` and `mfa_at` (the session's proof, copied on rotation), the CHECK
   added NOT VALID then validated.
10. RLS ENABLE + FORCE on the four tables: `*_self` (app_rw, own rows through `app.user_id`),
    `mc_hash` (app_rw, the challenge named by `app.challenge_hash`), `t_platform`, `t_migrator`.
    app_rw loses TRUNCATE; the read-only roles never see a secret, hash or password column.
11. `redact_user` and `purge_tenant` redefined (`sql/0011/01_erasure.up.sql`) to clear the four
    tables for an erased user; the downgrade restores the 0004 bodies first.

Downgrade (local and CI only, RULES.md "No prod downgrade") undoes it all in reverse.

Revision ID: 0011_mfa_lockout
Revises: 0010_signup_requests
Create Date: 2026-10-27

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_mfa_lockout"
down_revision: str | Sequence[str] | None = "0010_signup_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER: Final = "(SELECT public.app_user_id())"
CHALLENGE: Final = "(SELECT public.app_challenge_hash())"
READ_ONLY_ROLES: Final = "app_ro, app_platform_ro"
APP_ROLES: Final = "app_rw, app_platform_ro, app_ro"
SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0011"
STAFF_OR_SUPERUSER: Final = (
    "current_user = 'migrator' OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r "
    "WHERE r.rolname = current_user), false)"
)

USERS_UP: Final[tuple[str, ...]] = (
    """
    ALTER TABLE public.users
      ADD COLUMN failed_auth_count integer NOT NULL DEFAULT 0,
      ADD COLUMN locked_until timestamptz,
      ADD COLUMN mfa_last_changed_at timestamptz,
      ADD COLUMN password_changed_at timestamptz NOT NULL DEFAULT now(),
      ADD COLUMN password_must_change boolean NOT NULL DEFAULT false
    """,
    "ALTER TABLE public.users ADD CONSTRAINT ck_users_failed_auth_count "
    "CHECK (failed_auth_count BETWEEN 0 AND 10) NOT VALID",
    "GRANT SELECT (failed_auth_count, locked_until, mfa_last_changed_at, password_changed_at, "
    f"password_must_change) ON public.users TO {READ_ONLY_ROLES}",
    f"""
    CREATE FUNCTION public.users_auth_state_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF (TG_OP = 'INSERT' AND (NEW.failed_auth_count <> 0 OR NEW.locked_until IS NOT NULL))
         OR (TG_OP = 'UPDATE'
             AND (NEW.failed_auth_count IS DISTINCT FROM OLD.failed_auth_count
                  OR NEW.locked_until IS DISTINCT FROM OLD.locked_until)) THEN
        IF NOT ({STAFF_OR_SUPERUSER}) THEN
          RAISE EXCEPTION 'users lockout state moves only through its functions'
            USING ERRCODE = 'GRD01';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER users_auth_state_guard BEFORE INSERT OR UPDATE OF failed_auth_count, "
    "locked_until ON public.users FOR EACH ROW EXECUTE FUNCTION public.users_auth_state_guard()",
)
USERS_DOWN: Final[tuple[str, ...]] = (
    "DROP TRIGGER users_auth_state_guard ON public.users",
    "DROP FUNCTION public.users_auth_state_guard()",
    "ALTER TABLE public.users DROP CONSTRAINT ck_users_failed_auth_count",
    """
    ALTER TABLE public.users
      DROP COLUMN password_must_change,
      DROP COLUMN password_changed_at,
      DROP COLUMN mfa_last_changed_at,
      DROP COLUMN locked_until,
      DROP COLUMN failed_auth_count
    """,
)

# The lock threshold (10) and length (900 seconds) are literals here and constants in
# app/services/sign_in_policy.py; tests/unit/test_sign_in_decision.py keeps them equal.
FUNCTIONS_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.app_begin_auth_attempt(p_user uuid)
      RETURNS TABLE (failed_auth_count integer, locked_until timestamptz)
      LANGUAGE sql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
      SET lock_timeout = '5s'
    AS $$
      SELECT u.failed_auth_count, u.locked_until FROM public.users u
       WHERE u.id = p_user
         FOR UPDATE
    $$
    """,
    """
    CREATE FUNCTION public.app_note_auth_failure(p_user uuid) RETURNS boolean
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      locked_now boolean;
    BEGIN
      UPDATE public.users u
         SET failed_auth_count = CASE WHEN u.failed_auth_count + 1 >= 10 THEN 0
                                      ELSE u.failed_auth_count + 1 END,
             locked_until = CASE WHEN u.failed_auth_count + 1 >= 10
                                 THEN now() + interval '900 seconds' ELSE NULL END
       WHERE u.id = p_user AND (u.locked_until IS NULL OR u.locked_until <= now())
      RETURNING u.locked_until IS NOT NULL INTO locked_now;
      RETURN coalesce(locked_now, false);
    END
    $$
    """,
    """
    CREATE FUNCTION public.app_clear_auth_failures(p_user uuid) RETURNS void
      LANGUAGE sql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
      UPDATE public.users u SET failed_auth_count = 0, locked_until = NULL
       WHERE u.id = p_user AND p_user = public.app_user_id()
         AND (u.failed_auth_count <> 0 OR u.locked_until IS NOT NULL)
    $$
    """,
    "REVOKE ALL ON FUNCTION public.app_begin_auth_attempt(uuid), "
    "public.app_note_auth_failure(uuid), public.app_clear_auth_failures(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.app_begin_auth_attempt(uuid), "
    "public.app_note_auth_failure(uuid), public.app_clear_auth_failures(uuid) TO app_rw",
    """
    CREATE FUNCTION public.app_challenge_hash() RETURNS bytea LANGUAGE sql STABLE PARALLEL SAFE
    AS $$
      SELECT pg_catalog.decode(
        nullif(pg_catalog.current_setting('app.challenge_hash', true), ''), 'hex')
    $$
    """,
    "REVOKE ALL ON FUNCTION public.app_challenge_hash() FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION public.app_challenge_hash() TO {APP_ROLES}",
)
FUNCTIONS_DOWN: Final[tuple[str, ...]] = (
    "DROP FUNCTION public.app_challenge_hash()",
    "DROP FUNCTION public.app_clear_auth_failures(uuid)",
    "DROP FUNCTION public.app_note_auth_failure(uuid)",
    "DROP FUNCTION public.app_begin_auth_attempt(uuid)",
)

TABLES_UP: Final[tuple[str, ...]] = (
    """
    CREATE TABLE public.user_mfa_factors (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL,
      kind text NOT NULL DEFAULT 'totp',
      status text NOT NULL DEFAULT 'pending',
      secret_blob bytea NOT NULL,
      key_version text NOT NULL,
      last_used_step bigint,
      created_at timestamptz NOT NULL DEFAULT now(),
      confirmed_at timestamptz,
      last_used_at timestamptz,
      CONSTRAINT pk_user_mfa_factors PRIMARY KEY (id),
      CONSTRAINT fk_user_mfa_factors_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE,
      CONSTRAINT ck_user_mfa_factors_kind CHECK (kind = 'totp'),
      CONSTRAINT ck_user_mfa_factors_status CHECK (status IN ('pending', 'active')),
      CONSTRAINT ck_user_mfa_factors_secret_blob
        CHECK (octet_length(secret_blob) BETWEEN 1 AND 2048),
      CONSTRAINT ck_user_mfa_factors_key_version CHECK (char_length(key_version) <= 512)
    )
    """,
    "CREATE UNIQUE INDEX uq_user_mfa_factors_active ON public.user_mfa_factors (user_id, kind) "
    "WHERE status = 'active'",
    "CREATE UNIQUE INDEX uq_user_mfa_factors_pending ON public.user_mfa_factors (user_id, kind) "
    "WHERE status = 'pending'",
    f"""
    CREATE FUNCTION public.user_mfa_factors_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF {STAFF_OR_SUPERUSER} THEN
        RETURN NEW;
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.last_used_step IS NOT NULL
           OR NEW.confirmed_at IS NOT NULL THEN
          RAISE EXCEPTION 'a new MFA factor starts pending and unused' USING ERRCODE = 'GRD01';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.secret_blob IS DISTINCT FROM OLD.secret_blob
         OR NEW.key_version IS DISTINCT FROM OLD.key_version
         OR NEW.user_id IS DISTINCT FROM OLD.user_id
         OR NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'an MFA factor secret is never rewritten' USING ERRCODE = 'GRD01';
      END IF;
      IF OLD.last_used_step IS NOT NULL
         AND (NEW.last_used_step IS NULL OR NEW.last_used_step < OLD.last_used_step) THEN
        RAISE EXCEPTION 'last_used_step never decreases' USING ERRCODE = 'GRD01';
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER user_mfa_factors_guard BEFORE INSERT OR UPDATE ON public.user_mfa_factors "
    "FOR EACH ROW EXECUTE FUNCTION public.user_mfa_factors_guard()",
    """
    CREATE TABLE public.user_recovery_codes (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL,
      code_hash bytea NOT NULL,
      used_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_user_recovery_codes PRIMARY KEY (id),
      CONSTRAINT fk_user_recovery_codes_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE,
      CONSTRAINT uq_user_recovery_codes_code_hash UNIQUE (code_hash),
      CONSTRAINT ck_user_recovery_codes_code_hash CHECK (octet_length(code_hash) = 32)
    )
    """,
    "CREATE INDEX ix_user_recovery_codes_user_id ON public.user_recovery_codes (user_id) "
    "WHERE used_at IS NULL",
    """
    CREATE TABLE public.mfa_challenges (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      challenge_hash bytea NOT NULL,
      user_id uuid NOT NULL,
      purpose text NOT NULL,
      amr text[] NOT NULL,
      attempts smallint NOT NULL DEFAULT 0,
      expires_at timestamptz NOT NULL,
      consumed_at timestamptz,
      mfa_at timestamptz,
      ip inet,
      user_agent text,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_mfa_challenges PRIMARY KEY (id),
      CONSTRAINT fk_mfa_challenges_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE,
      CONSTRAINT uq_mfa_challenges_challenge_hash UNIQUE (challenge_hash),
      CONSTRAINT ck_mfa_challenges_challenge_hash CHECK (octet_length(challenge_hash) = 32),
      CONSTRAINT ck_mfa_challenges_purpose
        CHECK (purpose IN ('login', 'enroll', 'password_change')),
      CONSTRAINT ck_mfa_challenges_amr
        CHECK (amr <@ ARRAY['pwd', 'otp', 'rc']::text[] AND cardinality(amr) BETWEEN 1 AND 3),
      CONSTRAINT ck_mfa_challenges_attempts CHECK (attempts BETWEEN 0 AND 5),
      CONSTRAINT ck_mfa_challenges_mfa_at
        CHECK ((mfa_at IS NOT NULL) = (amr && ARRAY['otp', 'rc']::text[])),
      CONSTRAINT ck_mfa_challenges_user_agent CHECK (char_length(user_agent) <= 512)
    )
    """,
    "CREATE INDEX ix_mfa_challenges_user_id ON public.mfa_challenges (user_id)",
    """
    CREATE TABLE public.user_password_history (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL,
      hashed_password text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_user_password_history PRIMARY KEY (id),
      CONSTRAINT fk_user_password_history_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_user_password_history_user_id_created_at "
    "ON public.user_password_history (user_id, created_at DESC)",
    "REVOKE TRUNCATE ON public.user_mfa_factors, public.user_recovery_codes, "
    "public.mfa_challenges, public.user_password_history FROM app_rw",
    "REVOKE UPDATE ON public.user_password_history FROM app_rw",
    "GRANT SELECT (id, user_id, kind, status, key_version, last_used_step, created_at, "
    f"confirmed_at, last_used_at) ON public.user_mfa_factors TO {READ_ONLY_ROLES}",
    "GRANT SELECT (id, user_id, used_at, created_at) ON public.user_recovery_codes "
    f"TO {READ_ONLY_ROLES}",
    "GRANT SELECT (id, user_id, purpose, amr, attempts, expires_at, consumed_at, mfa_at, "
    f"created_at) ON public.mfa_challenges TO {READ_ONLY_ROLES}",
)
TABLES_DOWN: Final[tuple[str, ...]] = (
    "DROP TABLE public.user_password_history",
    "DROP TABLE public.mfa_challenges",
    "DROP TABLE public.user_recovery_codes",
    "DROP TABLE public.user_mfa_factors",
    "DROP FUNCTION public.user_mfa_factors_guard()",
)

REFRESH_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.refresh_tokens ADD COLUMN amr text[], ADD COLUMN mfa_at timestamptz",
    "ALTER TABLE public.refresh_tokens ADD CONSTRAINT ck_refresh_tokens_amr CHECK (amr IS NULL "
    "OR (amr <@ ARRAY['pwd', 'otp', 'rc']::text[] AND cardinality(amr) BETWEEN 1 AND 3)) "
    "NOT VALID",
    f"GRANT SELECT (amr, mfa_at) ON public.refresh_tokens TO {READ_ONLY_ROLES}",
)
REFRESH_DOWN: Final[tuple[str, ...]] = (
    "ALTER TABLE public.refresh_tokens DROP CONSTRAINT ck_refresh_tokens_amr",
    "ALTER TABLE public.refresh_tokens DROP COLUMN mfa_at, DROP COLUMN amr",
)

MFA_TABLES: Final = (
    "user_mfa_factors",
    "user_recovery_codes",
    "mfa_challenges",
    "user_password_history",
)
SELF_POLICY: Final = {
    "user_mfa_factors": "umf_self",
    "user_recovery_codes": "urc_self",
    "mfa_challenges": "mc_self",
    "user_password_history": "uph_self",
}


def _policies() -> tuple[tuple[str, str, str], ...]:
    """(table, policy name, statement) for the four tables."""
    rows: list[tuple[str, str, str]] = []
    for table in MFA_TABLES:
        name = SELF_POLICY[table]
        rows.append(
            (
                table,
                name,
                f"CREATE POLICY {name} ON public.{table} TO app_rw USING (user_id = {USER}) "
                f"WITH CHECK (user_id = {USER})",
            )
        )
        if table == "mfa_challenges":
            rows.append(
                (
                    table,
                    "mc_hash",
                    "CREATE POLICY mc_hash ON public.mfa_challenges TO app_rw "
                    f"USING (challenge_hash = {CHALLENGE}) WITH CHECK (challenge_hash = {CHALLENGE})",
                )
            )
        rows.append(
            (
                table,
                "t_platform",
                f"CREATE POLICY t_platform ON public.{table} FOR SELECT TO {READ_ONLY_ROLES} "
                "USING (current_user IN ('app_platform_ro', 'app_ro'))",
            )
        )
        rows.append(
            (
                table,
                "t_migrator",
                f"CREATE POLICY t_migrator ON public.{table} TO migrator "
                "USING (true) WITH CHECK (true)",
            )
        )
    return tuple(rows)


POLICIES: Final = _policies()
# Validated after the main transaction commits (the 0009 pattern): the ACCESS EXCLUSIVE of the ADDs
# is gone, so each scan holds only SHARE UPDATE EXCLUSIVE and blocks no reader or writer.
VALIDATE: Final[tuple[str, ...]] = (
    "ALTER TABLE public.users VALIDATE CONSTRAINT ck_users_failed_auth_count",
    "ALTER TABLE public.refresh_tokens VALIDATE CONSTRAINT ck_refresh_tokens_amr",
)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004), so `text()` never reads one as a bind parameter.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in (*USERS_UP, *FUNCTIONS_UP, *TABLES_UP, *REFRESH_UP):
        op.execute(statement)
    for table in MFA_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    for _, _, statement in POLICIES:
        op.execute(statement)
    _run_sql("01_erasure.up.sql")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for statement in VALIDATE:
            op.execute(statement)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_erasure.down.sql")
    for table, name, _ in reversed(POLICIES):
        op.execute(f"DROP POLICY {name} ON public.{table}")
    for statement in (*REFRESH_DOWN, *TABLES_DOWN, *FUNCTIONS_DOWN, *USERS_DOWN):
        op.execute(statement)
