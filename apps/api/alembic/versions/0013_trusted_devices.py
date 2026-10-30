"""trusted_devices: trusted browsers that may stand in for the login code

ADR-0023 (proposed; amends ADR-0021 §5 and §6 and ADR-0020 §5). Ledger row claimed in CANON.md
with the Tech Lead's OK on 2026-10-30. Expand only, as migrator, in one transaction plus a
trailing autocommit block (the CHECKs added NOT VALID to existing tables are validated there, the
0011/0012 pattern):

1. `trusted_devices`: a platform table (no `tenant_id`; a named invariant-5 exception, like
   `user_mfa_factors`), one row per browser and user. Only the sha256 of the browser's random
   token is stored (`token_hash`), with the previous one (`prev_token_hash`) so a rotated token
   presented again is caught; a fixed expiry of at most 30 days (`ck_trusted_devices_expiry`).
   ENABLE + FORCE RLS: app_rw reaches only `app.user_id`'s rows (`td_self`); app_rw may update
   only `token_hash`, `prev_token_hash` and `last_used_at`, and never truncates. The read-only
   roles never read a hash.
2. `trusted_devices_guard` (migrator and superusers exempt, GRD01 otherwise): a new row is
   trusted from a recent code (`trusted_at` is the code's time: at most 15 minutes ago, the
   forced password change included, and not ahead of the clock), unused, with no previous hash, and lasts at most 7 days when its
   owner is a platform admin (an unreadable owner counts as an admin); an update changes only the
   token, and a new token keeps the old one as `prev_token_hash`.
3. `mfa_challenges.trust_device`: "trust this browser" chosen at a code step, carried to the
   forced password change; only on a challenge that proved a TOTP code (CHECK).
4. `refresh_tokens.device_id` and `device_auth_at`: a family begun by a trusted browser and when
   (the 7-day rule counts from that sign-in while the browser stays trusted). Deliberately no
   foreign key: a removed browser must not turn its sessions into ordinary ones. `device_auth_at`
   without `device_id` is a session whose browser trust was dropped while the session itself was
   kept (sign out all, a password change from settings).
5. `app_prune_trusted_devices()` SECURITY DEFINER (owner migrator, pinned search_path, EXECUTE
   for app_rw only): deletes expired rows; the daily retention run.
6. `redact_user` and `purge_tenant` redefined (`sql/0013/01_erasure.up.sql`) to delete the erased
   user's trusted browsers.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores 0012's erasure bodies, then
undoes the rest in reverse.

Revision ID: 0013_trusted_devices
Revises: 0012_google_signin
Create Date: 2026-10-30

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_trusted_devices"
down_revision: str | Sequence[str] | None = "0012_google_signin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER: Final = "(SELECT public.app_user_id())"
READ_ONLY_ROLES: Final = "app_ro, app_platform_ro"
SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0013"
STAFF_OR_SUPERUSER: Final = (
    "current_user = 'migrator' OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r "
    "WHERE r.rolname = current_user), false)"
)
# sign_in_policy.TRUST_DAYS_BRAND and TRUST_DAYS_ADMIN; a unit test keeps them equal
TRUST_MAX: Final = "interval '30 days'"
TRUST_MAX_ADMIN: Final = "interval '7 days'"

TABLE_UP: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE public.trusted_devices (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL,
      token_hash bytea NOT NULL,
      prev_token_hash bytea,
      method text NOT NULL,
      label text NOT NULL,
      trusted_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      last_used_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_trusted_devices PRIMARY KEY (id),
      CONSTRAINT fk_trusted_devices_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE,
      CONSTRAINT uq_trusted_devices_token_hash UNIQUE (token_hash),
      CONSTRAINT ck_trusted_devices_hashes
        CHECK (octet_length(token_hash) = 32
               AND (prev_token_hash IS NULL OR octet_length(prev_token_hash) = 32)),
      CONSTRAINT ck_trusted_devices_method CHECK (method = 'otp'),
      CONSTRAINT ck_trusted_devices_label CHECK (char_length(label) BETWEEN 1 AND 64),
      CONSTRAINT ck_trusted_devices_expiry
        CHECK (expires_at > trusted_at AND expires_at <= trusted_at + {TRUST_MAX})
    )
    """,
    "CREATE INDEX ix_trusted_devices_user_id_created_at "
    "ON public.trusted_devices (user_id, created_at)",
    "CREATE INDEX ix_trusted_devices_expires_at ON public.trusted_devices (expires_at)",
    "REVOKE UPDATE, TRUNCATE ON public.trusted_devices FROM app_rw",
    "GRANT UPDATE (token_hash, prev_token_hash, last_used_at) ON public.trusted_devices TO app_rw",
    "GRANT SELECT (id, user_id, method, label, trusted_at, expires_at, last_used_at, created_at) "
    f"ON public.trusted_devices TO {READ_ONLY_ROLES}",
    "ALTER TABLE public.trusted_devices ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.trusted_devices FORCE ROW LEVEL SECURITY",
)
TABLE_DOWN: Final[tuple[str, ...]] = ("DROP TABLE public.trusted_devices",)

# (policy name, statement), all on trusted_devices
POLICIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "td_self",
        f"CREATE POLICY td_self ON public.trusted_devices TO app_rw USING (user_id = {USER}) "
        f"WITH CHECK (user_id = {USER})",
    ),
    (
        "t_platform",
        f"CREATE POLICY t_platform ON public.trusted_devices FOR SELECT TO {READ_ONLY_ROLES} "
        "USING (current_user IN ('app_platform_ro', 'app_ro'))",
    ),
    (
        "t_migrator",
        "CREATE POLICY t_migrator ON public.trusted_devices TO migrator "
        "USING (true) WITH CHECK (true)",
    ),
)

GUARDS_UP: Final[tuple[str, ...]] = (
    f"""
    CREATE FUNCTION public.trusted_devices_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      owner_admin boolean;
    BEGIN
      IF {STAFF_OR_SUPERUSER} THEN
        RETURN NEW;
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.last_used_at IS NOT NULL OR NEW.prev_token_hash IS NOT NULL
           OR NEW.trusted_at < now() - interval '15 minutes'
           OR NEW.trusted_at > now() + interval '60 seconds' THEN
          RAISE EXCEPTION 'a trusted browser starts from a recent code, unused, with one token'
            USING ERRCODE = 'GRD01';
        END IF;
        SELECT u.is_platform_admin INTO owner_admin FROM public.users u WHERE u.id = NEW.user_id;
        IF coalesce(owner_admin, true) AND NEW.expires_at > NEW.trusted_at + {TRUST_MAX_ADMIN} THEN
          RAISE EXCEPTION 'a platform admin trusts a browser for at most 7 days'
            USING ERRCODE = 'GRD01';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.id IS DISTINCT FROM OLD.id
         OR NEW.user_id IS DISTINCT FROM OLD.user_id
         OR NEW.method IS DISTINCT FROM OLD.method
         OR NEW.label IS DISTINCT FROM OLD.label
         OR NEW.trusted_at IS DISTINCT FROM OLD.trusted_at
         OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
         OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'a trusted browser changes only its token' USING ERRCODE = 'GRD01';
      END IF;
      IF NEW.token_hash IS DISTINCT FROM OLD.token_hash THEN
        IF NEW.prev_token_hash IS DISTINCT FROM OLD.token_hash THEN
          RAISE EXCEPTION 'a new token keeps the old one as prev_token_hash'
            USING ERRCODE = 'GRD01';
        END IF;
      ELSIF NEW.prev_token_hash IS DISTINCT FROM OLD.prev_token_hash THEN
        RAISE EXCEPTION 'prev_token_hash changes only with the token' USING ERRCODE = 'GRD01';
      END IF;
      RETURN NEW;
    END
    $$
    """,  # noqa: S608 - module constants only, no input
    "CREATE TRIGGER trusted_devices_guard BEFORE INSERT OR UPDATE ON public.trusted_devices "
    "FOR EACH ROW EXECUTE FUNCTION public.trusted_devices_guard()",
)
GUARDS_DOWN: Final[tuple[str, ...]] = (
    "DROP TRIGGER trusted_devices_guard ON public.trusted_devices",
    "DROP FUNCTION public.trusted_devices_guard()",
)

COLUMNS_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.mfa_challenges ADD COLUMN trust_device boolean",
    "ALTER TABLE public.mfa_challenges ADD CONSTRAINT ck_mfa_challenges_trust_device "
    "CHECK (trust_device IS NOT TRUE OR 'otp' = ANY (amr)) NOT VALID",
    f"GRANT SELECT (trust_device) ON public.mfa_challenges TO {READ_ONLY_ROLES}",
    "ALTER TABLE public.refresh_tokens ADD COLUMN device_id uuid, "
    "ADD COLUMN device_auth_at timestamptz",
    "ALTER TABLE public.refresh_tokens ADD CONSTRAINT ck_refresh_tokens_device "
    "CHECK (device_id IS NULL OR device_auth_at IS NOT NULL) NOT VALID",
    f"GRANT SELECT (device_id, device_auth_at) ON public.refresh_tokens TO {READ_ONLY_ROLES}",
)
COLUMNS_DOWN: Final[tuple[str, ...]] = (
    "ALTER TABLE public.refresh_tokens DROP CONSTRAINT ck_refresh_tokens_device",
    "ALTER TABLE public.refresh_tokens DROP COLUMN device_auth_at, DROP COLUMN device_id",
    "ALTER TABLE public.mfa_challenges DROP CONSTRAINT ck_mfa_challenges_trust_device",
    "ALTER TABLE public.mfa_challenges DROP COLUMN trust_device",
)

DEFINERS: Final[tuple[str, ...]] = ("public.app_prune_trusted_devices()",)
FUNCTIONS_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.app_prune_trusted_devices() RETURNS integer
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      n integer;
    BEGIN
      DELETE FROM public.trusted_devices d WHERE d.expires_at <= now();
      GET DIAGNOSTICS n = ROW_COUNT;
      RETURN n;
    END
    $$
    """,
    f"REVOKE ALL ON FUNCTION {', '.join(DEFINERS)} FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {', '.join(DEFINERS)} TO app_rw",
)
FUNCTIONS_DOWN: Final[tuple[str, ...]] = tuple(f"DROP FUNCTION {name}" for name in DEFINERS)

# Validated after the main transaction commits (the 0009, 0011 and 0012 pattern).
VALIDATE: Final[tuple[str, ...]] = (
    "ALTER TABLE public.mfa_challenges VALIDATE CONSTRAINT ck_mfa_challenges_trust_device",
    "ALTER TABLE public.refresh_tokens VALIDATE CONSTRAINT ck_refresh_tokens_device",
)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 and 0012), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in (*TABLE_UP, *GUARDS_UP):
        op.execute(statement)
    for _, statement in POLICIES:
        op.execute(statement)
    for statement in (*COLUMNS_UP, *FUNCTIONS_UP):
        op.execute(statement)
    _run_sql("01_erasure.up.sql")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for statement in VALIDATE:
            op.execute(statement)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_erasure.down.sql")
    for statement in (*FUNCTIONS_DOWN, *COLUMNS_DOWN):
        op.execute(statement)
    for name, _ in reversed(POLICIES):
        op.execute(f"DROP POLICY {name} ON public.trusted_devices")
    for statement in (*GUARDS_DOWN, *TABLE_DOWN):
        op.execute(statement)
