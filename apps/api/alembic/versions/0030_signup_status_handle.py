"""signup_status_handle: a read-only status handle for a pending sign-up (ADR-0019 amendment)

Ledger row claimed in CANON.md under the V1 delegation (client tester, 11 Nov 2026: a pending
applicant had to sign in again to learn whether the brand was approved). Expand only, as
migrator, in one transaction plus a trailing autocommit block (one VALIDATE and one CONCURRENTLY
index, the 0012 pattern):

1. `signup_requests.status_handle_hash bytea` (the sha256 of the opaque handle the applicant's
   browser keeps; never the handle itself) and `signup_requests.status_handle_expires_at
   timestamptz`. `ck_signup_requests_status_handle`: both set or both NULL, and the hash is 32
   bytes (NOT VALID, then validated). Neither column is granted to the read-only roles, and
   app_rw has no UPDATE on them: only the definer below writes them.
2. `uq_signup_requests_status_handle_hash` (partial, WHERE the hash is set), built CONCURRENTLY:
   the poll finds the one row by its hash.
3. `sr_handle_select` (app_rw, SELECT): the row whose live handle hash is `app.token_hash`
   (the `invitations_hash` pattern), and only while `status_handle_expires_at > now()`.
4. `app_set_signup_status_handle(uuid, bytea)`: SECURITY DEFINER (owner migrator, pinned
   search_path, EXECUTE for app_rw only). Sets the hash and an expiry of now() + 30 days on the
   caller's own (`app.user_id`) pending request, replacing an older handle; false for any other
   row, a decided request or a hash that is not 32 bytes.

Downgrade (local and CI only, RULES.md "No prod downgrade") drops the index, the function, the
policy, the CHECK and the two columns.

Revision ID: 0030_signup_status_handle
Revises: 0028_photo_read_on_upload
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_signup_status_handle"
# The single head on main (0028_photo_read_on_upload); re-chained onto 0029_admin_google_link
# if that lands first.
down_revision: str | Sequence[str] | None = "0028_photo_read_on_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_GUC: Final = "(SELECT public.app_token_hash())"
DEFINER: Final = "public.app_set_signup_status_handle(uuid, bytea)"

COLUMNS_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.signup_requests ADD COLUMN status_handle_hash bytea",
    "ALTER TABLE public.signup_requests ADD COLUMN status_handle_expires_at timestamptz",
    "ALTER TABLE public.signup_requests ADD CONSTRAINT ck_signup_requests_status_handle "
    "CHECK ((status_handle_hash IS NULL) = (status_handle_expires_at IS NULL) "
    "AND (status_handle_hash IS NULL OR octet_length(status_handle_hash) = 32)) NOT VALID",
)
COLUMNS_DOWN: Final[tuple[str, ...]] = (
    "ALTER TABLE public.signup_requests DROP CONSTRAINT ck_signup_requests_status_handle",
    "ALTER TABLE public.signup_requests DROP COLUMN status_handle_expires_at",
    "ALTER TABLE public.signup_requests DROP COLUMN status_handle_hash",
)

POLICY_NAME: Final = "sr_handle_select"
POLICY: Final = (
    f"CREATE POLICY {POLICY_NAME} ON public.signup_requests FOR SELECT TO app_rw "
    f"USING (status_handle_hash = {HASH_GUC} AND status_handle_expires_at > now())"
)

# Plain text, no interpolation: the 30-day expiry and the caller check are literals here.
FUNCTION_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.app_set_signup_status_handle(p_request uuid, p_hash bytea)
      RETURNS boolean
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      issued boolean;
    BEGIN
      IF p_hash IS NULL OR octet_length(p_hash) <> 32 THEN
        RETURN false;
      END IF;
      UPDATE public.signup_requests r
         SET status_handle_hash = p_hash,
             status_handle_expires_at = now() + interval '30 days'
       WHERE r.id = p_request AND r.user_id = (SELECT public.app_user_id()) AND r.status = 'pending'
      RETURNING true INTO issued;
      RETURN coalesce(issued, false);
    END
    $$
    """,
    f"REVOKE ALL ON FUNCTION {DEFINER} FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {DEFINER} TO app_rw",
)
FUNCTION_DOWN: Final[tuple[str, ...]] = (f"DROP FUNCTION {DEFINER}",)

VALIDATE: Final = (
    "ALTER TABLE public.signup_requests VALIDATE CONSTRAINT ck_signup_requests_status_handle"
)
INDEX_NAME: Final = "uq_signup_requests_status_handle_hash"
INDEX: Final = (
    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    "ON public.signup_requests (status_handle_hash) WHERE status_handle_hash IS NOT NULL"
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in COLUMNS_UP:
        op.execute(statement)
    op.execute(POLICY)
    for statement in FUNCTION_UP:
        op.execute(statement)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(VALIDATE)
        # a rerun after an interrupted build replaces an invalid index
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX_NAME}")
        op.execute(INDEX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX_NAME}")
    op.execute("SET lock_timeout = '5s'")
    for statement in FUNCTION_DOWN:
        op.execute(statement)
    op.execute(f"DROP POLICY {POLICY_NAME} ON public.signup_requests")
    for statement in COLUMNS_DOWN:
        op.execute(statement)
