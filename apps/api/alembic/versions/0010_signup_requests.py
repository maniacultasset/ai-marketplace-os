"""signup_requests: self-serve brand sign-up with platform-admin approval

ADR-0019 (owner-approved 2026-10-11; supersedes ADR-0006 decision 5 for this point). Expand only,
as migrator:

- `users.is_platform_admin boolean NOT NULL DEFAULT false` (a constant default: catalog-only, no
  rewrite). The read-only roles get SELECT on the new column. app_rw keeps its table-level UPDATE
  on `users`, so the BEFORE INSERT OR UPDATE OF trigger `users_platform_admin_guard` refuses
  (GRD01) any change of the flag, and any INSERT with it set, unless the current role is
  `migrator` (the staff CLI `user make-admin`) or a superuser. A sign-up body cannot set it.
- `public.app_is_platform_admin()`: whether `app.user_id` names a platform admin. SECURITY
  DEFINER (owned by migrator, pinned search_path, EXECUTE for app_rw only), so the policies below
  read the flag without re-entering the `users` policies.
- `signup_requests` (a platform table, no `tenant_id`): `user_id → users` (CASCADE),
  `brand_name`, `owner_name`, `phone`, `website`, `status pending|approved|rejected`, `reason`,
  `decided_by_user_id → users`, `decided_at`, `created_tenant_id → tenants` (SET NULL, set on
  approve), `created_at`, `updated_at`. `uq_signup_requests_pending`: one pending request per
  user (partial unique index). `ix_signup_requests_status_created_at` serves the admin queue.
- RLS ENABLE + FORCE on `signup_requests`: `sr_self_select` / `sr_self_insert` (the applicant,
  through `app.user_id`; an insert must be a fresh pending row), `sr_admin` (app_rw, every
  command, only while `app_is_platform_admin()`), `t_platform` (SELECT for the read-only roles),
  `t_migrator`. `users_platform_admin`: a platform admin reads every user row (the request list
  shows the applicant's email).
- Grants: app_rw loses DELETE and TRUNCATE and keeps UPDATE only on the decision columns; the
  read-only roles read every column but `owner_name` and `phone` (personal data).

Downgrade drops the policies, the table, the function, the trigger and the column.

Revision ID: 0010_signup_requests
Revises: 0009_analytics_trace
Create Date: 2026-10-11

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_signup_requests"
down_revision: str | Sequence[str] | None = "0009_analytics_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER: Final = "(SELECT public.app_user_id())"
IS_ADMIN: Final = "(SELECT public.app_is_platform_admin())"
READ_ONLY_ROLES: Final = "app_ro, app_platform_ro"
SIGNUP_READABLE: Final = (
    "id, user_id, brand_name, website, status, reason, decided_by_user_id, decided_at, "
    "created_tenant_id, created_at, updated_at"
)
DECISION_COLUMNS: Final = "status, reason, decided_by_user_id, decided_at, created_tenant_id"

USERS_UP: Final[tuple[str, ...]] = (
    "ALTER TABLE public.users ADD COLUMN is_platform_admin boolean NOT NULL DEFAULT false",
    f"GRANT SELECT (is_platform_admin) ON public.users TO {READ_ONLY_ROLES}",
    """
    CREATE FUNCTION public.users_platform_admin_guard() RETURNS trigger
      LANGUAGE plpgsql
      SET search_path = pg_catalog, public, pg_temp
    AS $$
    BEGIN
      IF (TG_OP = 'INSERT' AND NEW.is_platform_admin)
         OR (TG_OP = 'UPDATE' AND NEW.is_platform_admin IS DISTINCT FROM OLD.is_platform_admin) THEN
        IF NOT (current_user = 'migrator'
                OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r
                              WHERE r.rolname = current_user), false)) THEN
          RAISE EXCEPTION 'users.is_platform_admin is set only by staff' USING ERRCODE = 'GRD01';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$
    """,
    "CREATE TRIGGER users_platform_admin_guard BEFORE INSERT OR UPDATE OF is_platform_admin "
    "ON public.users FOR EACH ROW EXECUTE FUNCTION public.users_platform_admin_guard()",
    """
    CREATE FUNCTION public.app_is_platform_admin() RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path = pg_catalog, public, pg_temp
    AS $$
      SELECT coalesce(
        (SELECT u.is_platform_admin AND u.is_active FROM public.users u
          WHERE u.id = public.app_user_id()),
        false)
    $$
    """,
    "REVOKE ALL ON FUNCTION public.app_is_platform_admin() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.app_is_platform_admin() TO app_rw",
)
USERS_DOWN: Final[tuple[str, ...]] = (
    "DROP FUNCTION public.app_is_platform_admin()",
    "DROP TRIGGER users_platform_admin_guard ON public.users",
    "DROP FUNCTION public.users_platform_admin_guard()",
    "ALTER TABLE public.users DROP COLUMN is_platform_admin",
)

TABLE_UP: Final[tuple[str, ...]] = (
    """
    CREATE TABLE public.signup_requests (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL,
      brand_name text NOT NULL,
      owner_name text NOT NULL,
      phone text,
      website text,
      status text NOT NULL DEFAULT 'pending',
      reason text,
      decided_by_user_id uuid,
      decided_at timestamptz,
      created_tenant_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_signup_requests PRIMARY KEY (id),
      CONSTRAINT fk_signup_requests_user_id_users FOREIGN KEY (user_id)
        REFERENCES public.users (id) ON DELETE CASCADE,
      CONSTRAINT fk_signup_requests_decided_by_user_id_users FOREIGN KEY (decided_by_user_id)
        REFERENCES public.users (id),
      CONSTRAINT fk_signup_requests_created_tenant_id_tenants FOREIGN KEY (created_tenant_id)
        REFERENCES public.tenants (id) ON DELETE SET NULL,
      CONSTRAINT ck_signup_requests_status
        CHECK (status IN ('pending', 'approved', 'rejected')),
      CONSTRAINT ck_signup_requests_decided
        CHECK ((status = 'pending') = (decided_at IS NULL)),
      CONSTRAINT ck_signup_requests_bounds CHECK (
        char_length(brand_name) BETWEEN 1 AND 200
        AND char_length(owner_name) BETWEEN 1 AND 200
        AND char_length(phone) <= 32
        AND char_length(website) <= 2048
        AND char_length(reason) <= 1000
      )
    )
    """,
    "CREATE UNIQUE INDEX uq_signup_requests_pending ON public.signup_requests (user_id) "
    "WHERE status = 'pending'",
    "CREATE INDEX ix_signup_requests_status_created_at ON public.signup_requests "
    "(status, created_at, id)",
    "CREATE INDEX ix_signup_requests_user_id ON public.signup_requests (user_id, created_at)",
    "CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.signup_requests "
    "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()",
    "REVOKE DELETE, TRUNCATE ON public.signup_requests FROM app_rw",
    "REVOKE UPDATE ON public.signup_requests FROM app_rw",
    f"GRANT UPDATE ({DECISION_COLUMNS}) ON public.signup_requests TO app_rw",
    f"GRANT SELECT ({SIGNUP_READABLE}) ON public.signup_requests TO {READ_ONLY_ROLES}",
)
TABLE_DOWN: Final[tuple[str, ...]] = ("DROP TABLE public.signup_requests",)

# (table, policy name, statement)
POLICIES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "signup_requests",
        "sr_self_select",
        "CREATE POLICY sr_self_select ON public.signup_requests FOR SELECT TO app_rw "
        f"USING (user_id = {USER})",
    ),
    (
        "signup_requests",
        "sr_self_insert",
        "CREATE POLICY sr_self_insert ON public.signup_requests FOR INSERT TO app_rw "
        f"WITH CHECK (user_id = {USER} AND status = 'pending' AND decided_by_user_id IS NULL "
        "AND decided_at IS NULL AND created_tenant_id IS NULL AND reason IS NULL)",
    ),
    (
        "signup_requests",
        "sr_admin",
        f"CREATE POLICY sr_admin ON public.signup_requests TO app_rw USING ({IS_ADMIN}) "
        f"WITH CHECK ({IS_ADMIN})",
    ),
    (
        "signup_requests",
        "t_platform",
        "CREATE POLICY t_platform ON public.signup_requests FOR SELECT "
        f"TO {READ_ONLY_ROLES} USING (current_user IN ('app_platform_ro', 'app_ro'))",
    ),
    (
        "signup_requests",
        "t_migrator",
        "CREATE POLICY t_migrator ON public.signup_requests TO migrator "
        "USING (true) WITH CHECK (true)",
    ),
    (
        "users",
        "users_platform_admin",
        "CREATE POLICY users_platform_admin ON public.users FOR SELECT TO app_rw "
        f"USING ({IS_ADMIN})",
    ),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for statement in (*USERS_UP, *TABLE_UP):
        op.execute(statement)
    op.execute("ALTER TABLE public.signup_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.signup_requests FORCE ROW LEVEL SECURITY")
    for _, _, statement in POLICIES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for table, name, _ in reversed(POLICIES):
        op.execute(f"DROP POLICY {name} ON public.{table}")
    for statement in (*TABLE_DOWN, *USERS_DOWN):
        op.execute(statement)
