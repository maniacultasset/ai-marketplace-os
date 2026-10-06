"""rls: row-level security, ENABLEd and FORCEd on every tenant table

DATABASE.md rev 3 §4 ("0005 template"), §8.2 ledger row 0005 and §11.2 rows 5, 6 and 19; CANON.md
ledger row 0005. Every table in `TENANT_TABLES` gets ENABLE + FORCE ROW LEVEL SECURITY and the
three-policy template: `t_tenant` (app_rw, its own tenant through the `app.tenant_id` GUC),
`t_platform` (SELECT for the read-only roles, shut for plain app_rw by the `current_user` guard)
and `t_migrator` (the owner, which FORCE no longer exempts). Each helper call is an initplan
`(SELECT app_…())`, evaluated once per statement. `tenants`, `users`, `rules` and `audit_log`
carry their own policy sets, and the named invariant-5 exceptions (identity and token branches,
`jr_by_id`) are listed in `BRANCHES`. The catalog assertion in the tests (RLS forced on every
table with a `tenant_id` column), not this list, is the real check.

The platform tables, `rate_limit_counters` and the procrastinate queue have no RLS. The Canon
"settings flag" is replaced by the migrator-run `scripts/db/rls_emergency_disable.sql`; recovery
is `alembic downgrade 0004_integrity` then `upgrade head`.

One 0004 function changes with RLS: `listing_issues_guard` refused a rule of *another* tenant by
`EXISTS`, which RLS turns fail-open (app_rw cannot see B's rules, and the plain FK bypasses RLS).
0005 replaces that check with the fail-closed `NOT EXISTS (platform or own-tenant rule)`, so a
hidden or missing rule is GRD01 alike; downgrade restores the 0004 body from its SQL file.

Revision ID: 0005_rls
Revises: 0004_integrity
Create Date: 2026-10-06

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_rls"
down_revision: str | Sequence[str] | None = "0004_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table holding a `tenant_id` that takes the plain three-policy template (DATABASE.md:1771).
TENANT_TABLES: Final[tuple[str, ...]] = (
    "tenant_memberships",
    "invitations",
    "brand_profiles",
    "business_parties",
    "products",
    "product_colourways",
    "variant_matrix_cells",
    "marketplace_accounts",
    "listings",
    "listing_versions",
    "images",
    "brand_documents",
    "brand_chunks",
    "review_decisions",
    "validation_runs",
    "agent_runs",
    "performance",
    "batches",
    "listing_items",
    "human_tasks",
    "listing_issues",
    "job_runs",
    "imports",
    "import_rows",
    "idempotency_keys",
    "refresh_tokens",
    "oauth_states",
    "listing_submissions",
    "publish_field_results",
)

TENANT: Final = "(SELECT public.app_tenant_id())"
PLATFORM_ROLES: Final = "app_platform_ro, app_ro"
PLATFORM_USING: Final = "(current_user IN ('app_platform_ro', 'app_ro'))"


def _template(table: str, column: str = "tenant_id") -> list[tuple[str, str]]:
    return [
        (
            "t_tenant",
            f"CREATE POLICY t_tenant ON public.{table} TO app_rw "
            f"USING ({column} = {TENANT}) WITH CHECK ({column} = {TENANT})",
        ),
        (
            "t_platform",
            f"CREATE POLICY t_platform ON public.{table} FOR SELECT TO {PLATFORM_ROLES} "
            f"USING {PLATFORM_USING}",
        ),
        (
            "t_migrator",
            f"CREATE POLICY t_migrator ON public.{table} TO migrator USING (true) WITH CHECK (true)",
        ),
    ]


def _platform_and_migrator(table: str) -> list[tuple[str, str]]:
    return [p for p in _template(table) if p[0] != "t_tenant"]


# The tables with their own policy set instead of the template.
OWN_POLICIES: Final[dict[str, list[tuple[str, str]]]] = {
    # DATABASE.md:230: `id = app_tenant_id()`.
    "tenants": _template("tenants", column="id"),
    # DATABASE.md:244: self; no DELETE for app_rw (the privilege layer).
    "users": [
        (
            "users_self",
            "CREATE POLICY users_self ON public.users TO app_rw "
            "USING (id = (SELECT public.app_user_id())) "
            "WITH CHECK (id = (SELECT public.app_user_id()))",
        ),
        *_platform_and_migrator("users"),
    ],
    # DATABASE.md:508: five policies.
    "rules": [
        (
            "rules_read",
            "CREATE POLICY rules_read ON public.rules FOR SELECT TO app_rw "
            f"USING (tenant_id IS NULL OR tenant_id = {TENANT})",
        ),
        (
            "rules_write",
            "CREATE POLICY rules_write ON public.rules FOR INSERT TO app_rw "
            f"WITH CHECK (tenant_id = {TENANT})",
        ),
        (
            "rules_toggle",
            "CREATE POLICY rules_toggle ON public.rules FOR UPDATE TO app_rw "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})",
        ),
        *_platform_and_migrator("rules"),
    ],
    # DATABASE.md:304: SELECT own tenant only; INSERT own or NULL (failed logins); no UPDATE.
    "audit_log": [
        (
            "audit_read",
            "CREATE POLICY audit_read ON public.audit_log FOR SELECT TO app_rw "
            f"USING (tenant_id = {TENANT})",
        ),
        (
            "audit_insert",
            "CREATE POLICY audit_insert ON public.audit_log FOR INSERT TO app_rw "
            f"WITH CHECK (tenant_id = {TENANT} OR tenant_id IS NULL)",
        ),
        *_platform_and_migrator("audit_log"),
    ],
}

USER: Final = "(SELECT public.app_user_id())"
LOGIN_EMAIL: Final = "(SELECT public.app_login_email())"
HASH_GUC: Final = "(SELECT public.app_token_hash())"

# The named invariant-5 exceptions (DATABASE.md:311, §11.2 row 19), added beside the template.
# Every branch but signup is SELECT-only, so a write still needs the tenant (or self) policy.
BRANCHES: Final[dict[str, list[tuple[str, str]]]] = {
    # DATABASE.md:244: login and signup on the login email; teammates via membership. The
    # membership policies read GUCs only, so these subqueries never recurse (DATABASE.md:258).
    "users": [
        (
            "users_login",
            "CREATE POLICY users_login ON public.users FOR SELECT TO app_rw "
            f"USING (lower(email) = {LOGIN_EMAIL})",
        ),
        (
            "users_signup",
            "CREATE POLICY users_signup ON public.users FOR INSERT TO app_rw "
            f"WITH CHECK (lower(email) = {LOGIN_EMAIL})",
        ),
        (
            "users_teammates",
            "CREATE POLICY users_teammates ON public.users FOR SELECT TO app_rw "
            "USING (EXISTS (SELECT 1 FROM public.tenant_memberships m "
            "WHERE m.user_id = users.id AND m.tenant_id = (SELECT public.app_tenant_id())))",
        ),
    ],
    # DATABASE.md:258: standard, plus own rows (the login lists every tenant of the caller).
    "tenant_memberships": [
        (
            "memberships_own",
            "CREATE POLICY memberships_own ON public.tenant_memberships FOR SELECT TO app_rw "
            f"USING (user_id = {USER})",
        ),
    ],
    # DATABASE.md:230: plus a switcher SELECT via the caller's active membership.
    "tenants": [
        (
            "tenants_switcher",
            "CREATE POLICY tenants_switcher ON public.tenants FOR SELECT TO app_rw "
            "USING (EXISTS (SELECT 1 FROM public.tenant_memberships m "
            "WHERE m.tenant_id = tenants.id AND m.user_id = (SELECT public.app_user_id()) "
            "AND m.is_active))",
        ),
    ],
    # DATABASE.md:283: plus a token-hash branch (the refresh before any GUC), plus own rows
    # ("log out everywhere" lists them).
    "refresh_tokens": [
        (
            "refresh_tokens_hash",
            "CREATE POLICY refresh_tokens_hash ON public.refresh_tokens FOR SELECT TO app_rw "
            f"USING (token_hash = {HASH_GUC})",
        ),
        (
            "refresh_tokens_own",
            "CREATE POLICY refresh_tokens_own ON public.refresh_tokens FOR SELECT TO app_rw "
            f"USING (user_id = {USER})",
        ),
    ],
    # DATABASE.md:271: plus a SELECT on the token hash for the pre-login accept step.
    "invitations": [
        (
            "invitations_hash",
            "CREATE POLICY invitations_hash ON public.invitations FOR SELECT TO app_rw "
            f"USING (token_hash = {HASH_GUC})",
        ),
    ],
    # The OAuth callback finds its state by hash before it knows the tenant (CODE-PLAN B3).
    "oauth_states": [
        (
            "oauth_states_hash",
            "CREATE POLICY oauth_states_hash ON public.oauth_states FOR SELECT TO app_rw "
            f"USING (state_hash = {HASH_GUC})",
        ),
    ],
    # DATABASE.md:950: the worker reads tenant_id by job id before any tenant GUC exists.
    "job_runs": [
        (
            "jr_by_id",
            "CREATE POLICY jr_by_id ON public.job_runs FOR SELECT TO app_rw "
            "USING (id = (SELECT public.app_job_id()))",
        ),
    ],
}


def _plan() -> list[tuple[str, list[tuple[str, str]]]]:
    plan = [(t, _template(t) + BRANCHES.get(t, [])) for t in TENANT_TABLES]
    plan += [(t, policies + BRANCHES.get(t, [])) for t, policies in OWN_POLICIES.items()]
    return plan


ISSUES_GUARDS_0004: Final = (
    Path(__file__).resolve().parents[1] / "sql" / "0004" / "12_issues_tasks_guards.up.sql"
)
ISSUES_GUARD_HEAD: Final = "CREATE FUNCTION public.listing_issues_guard()"
# The 0004 check, verbatim, and its fail-closed replacement: under RLS a rule the caller cannot
# see (another tenant's) and a missing rule both fail `NOT EXISTS`, so neither reaches the FK.
RULE_CHECK_0004: Final = """\
     AND EXISTS (SELECT 1 FROM public.rules r  -- a missing rule is left to the plain FK (23503)
                  WHERE r.id = NEW.rule_id AND r.tenant_id IS DISTINCT FROM NEW.tenant_id
                    AND r.tenant_id IS NOT NULL) THEN"""
RULE_CHECK_0005: Final = """\
     AND NOT EXISTS (SELECT 1 FROM public.rules r  -- fail closed: a rule hidden by RLS (another
                      WHERE r.id = NEW.rule_id     -- tenant's) or missing is GRD01, never the FK
                        AND (r.tenant_id IS NULL OR r.tenant_id = NEW.tenant_id)) THEN"""


def _issues_guard_0004() -> str:
    """`listing_issues_guard` exactly as 0004 created it, as a CREATE OR REPLACE."""
    text = ISSUES_GUARDS_0004.read_text(encoding="utf-8")
    start = text.index(ISSUES_GUARD_HEAD)
    end = text.index("$$;", start) + len("$$;")
    return "CREATE OR REPLACE" + text[start:end].removeprefix("CREATE")


def _issues_guard_0005() -> str:
    body = _issues_guard_0004()
    if body.count(RULE_CHECK_0004) != 1:
        raise RuntimeError("0004 listing_issues_guard rule check not found verbatim")
    return body.replace(RULE_CHECK_0004, RULE_CHECK_0005)


def _run_function(statement: str) -> None:
    # `:` would read as a bind parameter in sa.text (the `::regclass` casts), as in 0004.
    op.execute(sa.text(statement.replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for table, policies in _plan():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        for _, statement in policies:
            op.execute(statement)
    _run_function(_issues_guard_0005())


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_function(_issues_guard_0004())
    for table, policies in reversed(_plan()):
        for name, _ in reversed(policies):
            op.execute(f"DROP POLICY {name} ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
