"""signup_info_request: a platform admin asks one question, the applicant sends one reply (ADR-0019 amendment)

Ledger row claimed in CANON.md under the V1 delegation (client feedback item 2, 11 Nov 2026: a
reviewer could only approve or reject; a request that needs more information had to be rejected
and sent again). The request stays `pending` the whole time, so `ck_signup_requests_status`, the
one-pending index and every reader of the status are unchanged. Expand only, as migrator, in one
transaction plus a trailing autocommit block (one VALIDATE, the 0031 pattern):

1. `signup_requests.info_question text`, `info_asked_at timestamptz`, `info_reply text` and
   `info_replied_at timestamptz`. `ck_signup_requests_info`: each text with its time, a reply only
   after a question, each text 1 to 1000 characters (NOT VALID, then validated). None is granted
   to the read-only roles.
2. app_rw gets UPDATE on `info_question` and `info_asked_at` only (a platform admin, through
   `sr_admin`); it has no UPDATE on the reply columns.
3. `signup_requests_info_guard` (BEFORE INSERT OR UPDATE OF the question columns): GRD01 for an
   insert that sets any info column, and for a question written by anyone but a platform admin,
   on a request that is not pending, or over a question already asked (one question per request),
   unless the caller is migrator or a superuser.
4. `app_reply_signup_info(bytea, text)`: SECURITY DEFINER (owner migrator, pinned search_path,
   EXECUTE for app_rw only). For the request whose live status handle has this sha256: stores
   the one reply (`replied`), answers the same reply again (`same`), refuses a request with no
   open question, a decided request or a second, different reply (`conflict`), and answers
   `not_found` for an unknown or expired handle.

Downgrade (local and CI only, RULES.md "No prod downgrade") drops the function, the trigger, the
CHECK and the four columns (their grants go with them).

Revision ID: 0037_signup_info_request
Revises: 0036_section_locks
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037_signup_info_request"
# The single head on main after the wave A release train (#170).
down_revision: str | Sequence[str] | None = "0036_section_locks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INFO_MAX: Final = 1000  # REASON_MAX in app/api/v1/schemas/signup.py
DEFINER: Final = "public.app_reply_signup_info(bytea, text)"
QUESTION_COLUMNS: Final = "info_question, info_asked_at"

COLUMNS_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.signup_requests ADD COLUMN info_question text",
    "ALTER TABLE public.signup_requests ADD COLUMN info_asked_at timestamptz",
    "ALTER TABLE public.signup_requests ADD COLUMN info_reply text",
    "ALTER TABLE public.signup_requests ADD COLUMN info_replied_at timestamptz",
    "ALTER TABLE public.signup_requests ADD CONSTRAINT ck_signup_requests_info CHECK ("
    "(info_question IS NULL) = (info_asked_at IS NULL) "
    "AND (info_reply IS NULL) = (info_replied_at IS NULL) "
    "AND (info_reply IS NULL OR info_question IS NOT NULL) "
    f"AND (info_question IS NULL OR char_length(info_question) BETWEEN 1 AND {INFO_MAX}) "
    f"AND (info_reply IS NULL OR char_length(info_reply) BETWEEN 1 AND {INFO_MAX})) NOT VALID",
    f"GRANT UPDATE ({QUESTION_COLUMNS}) ON public.signup_requests TO app_rw",
)
COLUMNS_DOWN: Final[tuple[str, ...]] = (
    "ALTER TABLE public.signup_requests DROP CONSTRAINT ck_signup_requests_info",
    "ALTER TABLE public.signup_requests DROP COLUMN info_replied_at",
    "ALTER TABLE public.signup_requests DROP COLUMN info_reply",
    "ALTER TABLE public.signup_requests DROP COLUMN info_asked_at",
    "ALTER TABLE public.signup_requests DROP COLUMN info_question",
)

GUARD_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.signup_requests_info_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF current_user = 'migrator' OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r
         WHERE r.rolname = current_user), false) THEN
        RETURN NEW;
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.info_question IS NOT NULL OR NEW.info_asked_at IS NOT NULL
           OR NEW.info_reply IS NOT NULL OR NEW.info_replied_at IS NOT NULL THEN
          RAISE EXCEPTION 'signup_requests.info_* is not set at insert' USING ERRCODE = 'GRD01';
        END IF;
        RETURN NEW;
      END IF;
      IF (NEW.info_question IS DISTINCT FROM OLD.info_question
          OR NEW.info_asked_at IS DISTINCT FROM OLD.info_asked_at)
         AND (OLD.info_question IS NOT NULL OR OLD.status <> 'pending' OR NEW.status <> 'pending'
              OR NOT (SELECT public.app_is_platform_admin())) THEN
        RAISE EXCEPTION 'one question, by a platform admin, on a pending request'
          USING ERRCODE = 'GRD01';
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER signup_requests_info_guard BEFORE INSERT OR UPDATE OF "
    f"{QUESTION_COLUMNS} ON public.signup_requests "
    "FOR EACH ROW EXECUTE FUNCTION public.signup_requests_info_guard()",
)
GUARD_DOWN: Final[tuple[str, ...]] = (
    "DROP TRIGGER signup_requests_info_guard ON public.signup_requests",
    "DROP FUNCTION public.signup_requests_info_guard()",
)

# Plain text, no interpolation: the outcomes and the caller check are literals here.
FUNCTION_UP: Final[tuple[str, ...]] = (
    """
    CREATE FUNCTION public.app_reply_signup_info(p_hash bytea, p_reply text)
      RETURNS text
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
      found_row record;
    BEGIN
      IF p_hash IS NULL OR octet_length(p_hash) <> 32 OR p_reply IS NULL THEN
        RETURN 'not_found';
      END IF;
      SELECT r.id, r.status, r.info_question, r.info_reply INTO found_row
        FROM public.signup_requests r
       WHERE r.status_handle_hash = p_hash AND r.status_handle_expires_at > now()
       FOR UPDATE;
      IF NOT FOUND THEN
        RETURN 'not_found';
      END IF;
      IF found_row.status <> 'pending' OR found_row.info_question IS NULL THEN
        RETURN 'conflict';
      END IF;
      IF found_row.info_reply IS NOT NULL THEN
        RETURN CASE WHEN found_row.info_reply = p_reply THEN 'same' ELSE 'conflict' END;
      END IF;
      UPDATE public.signup_requests
         SET info_reply = p_reply, info_replied_at = now()
       WHERE id = found_row.id;
      RETURN 'replied';
    END
    $$
    """,
    f"REVOKE ALL ON FUNCTION {DEFINER} FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {DEFINER} TO app_rw",
)
FUNCTION_DOWN: Final[tuple[str, ...]] = (f"DROP FUNCTION {DEFINER}",)

VALIDATE: Final = "ALTER TABLE public.signup_requests VALIDATE CONSTRAINT ck_signup_requests_info"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in COLUMNS_UP:
        op.execute(statement)
    for statement in GUARD_UP:
        op.execute(statement)
    for statement in FUNCTION_UP:
        op.execute(statement)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(VALIDATE)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in FUNCTION_DOWN:
        op.execute(statement)
    for statement in GUARD_DOWN:
        op.execute(statement)
    for statement in COLUMNS_DOWN:
        op.execute(statement)
