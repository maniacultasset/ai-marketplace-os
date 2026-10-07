"""oauth pending: the sealed-token store between the OAuth callback and the confirm

DATABASE.md §5.4 `oauth_states` and §8.2 ledger row 0006; CANON.md ledger row 0006 (S15). The
public callback has no JWT, so it cannot create the account: it seals the exchanged tokens into a
*pending* row (a fresh opaque handle, stored as its sha256 in `state_hash`), and the
authenticated confirm by the user who started the flow consumes that row under the tenant policy.

- Six nullable columns, no default (catalog-only): `token_enc`, `kms_key_version`, `seller_id`,
  `region`, `scopes`, `token_expires_at`. The read-only roles keep their column-level SELECT
  (0003), so the new columns stay hidden from them.
- Three CHECKs, added NOT VALID and then validated (the table holds minutes of rows): a row is a
  state row (no token) or a complete pending row (token, key version and seller); a pending row
  carries no PKCE verifier; bounded sizes, and the seller id pattern `^[A-Za-z0-9]{1,64}$`.
- The hash branch: 0005's SELECT-only `oauth_states_hash` becomes `oauth_states_hash_select` and
  `oauth_states_hash_delete`, both `USING (state_hash = app_token_hash() AND token_enc IS NULL)`,
  so the callback's consume (`DELETE ... RETURNING`) works and a pending row is never visible to,
  or deletable by, the public path. No UPDATE or INSERT branch. The tenant policy is unchanged.

Downgrade: pending rows are deleted first (a restored 0005 branch would let the callback consume
one as a state row), then the two policies are replaced by 0005's `oauth_states_hash` verbatim and
the CHECKs and columns are dropped.

Revision ID: 0006_oauth_pending
Revises: 0005_rls
Create Date: 2026-10-07

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_oauth_pending"
down_revision: str | Sequence[str] | None = "0005_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_GUC: Final = "(SELECT public.app_token_hash())"
COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("token_enc", "bytea"),
    ("kms_key_version", "text"),
    ("seller_id", "text"),
    ("region", "text"),
    ("scopes", "text[]"),
    ("token_expires_at", "timestamptz"),
)
CHECKS: Final[tuple[tuple[str, str], ...]] = (
    (
        "ck_oauth_states_pending_complete",
        "(token_enc IS NULL) = (kms_key_version IS NULL) "
        "AND (token_enc IS NULL) = (seller_id IS NULL)",
    ),
    ("ck_oauth_states_pending_no_verifier", "token_enc IS NULL OR code_verifier IS NULL"),
    (
        "ck_oauth_states_pending_bounds",
        "octet_length(token_enc) <= 16384 "
        "AND seller_id ~ '^[A-Za-z0-9]{1,64}$' "
        "AND char_length(kms_key_version) <= 512 "
        "AND char_length(region) <= 32 "
        "AND cardinality(scopes) <= 64",
    ),
)
# 0005_rls's branch, restored verbatim on downgrade (0005_rls.py BRANCHES["oauth_states"]).
S10_HASH_POLICY: Final = (
    "CREATE POLICY oauth_states_hash ON public.oauth_states FOR SELECT TO app_rw "
    f"USING (state_hash = {HASH_GUC})"
)
HASH_POLICIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "oauth_states_hash_select",
        "CREATE POLICY oauth_states_hash_select ON public.oauth_states FOR SELECT TO app_rw "
        f"USING (state_hash = {HASH_GUC} AND token_enc IS NULL)",
    ),
    (
        "oauth_states_hash_delete",
        "CREATE POLICY oauth_states_hash_delete ON public.oauth_states FOR DELETE TO app_rw "
        f"USING (state_hash = {HASH_GUC} AND token_enc IS NULL)",
    ),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for name, sql_type in COLUMNS:
        op.execute(f"ALTER TABLE public.oauth_states ADD COLUMN {name} {sql_type}")
    for name, expression in CHECKS:
        op.execute(
            f"ALTER TABLE public.oauth_states ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID"
        )
        op.execute(f"ALTER TABLE public.oauth_states VALIDATE CONSTRAINT {name}")
    op.execute("DROP POLICY IF EXISTS oauth_states_hash ON public.oauth_states")
    for _, statement in HASH_POLICIES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DELETE FROM public.oauth_states WHERE token_enc IS NOT NULL")
    for name, _ in reversed(HASH_POLICIES):
        op.execute(f"DROP POLICY {name} ON public.oauth_states")
    op.execute(S10_HASH_POLICY)
    for name, _ in reversed(CHECKS):
        op.execute(f"ALTER TABLE public.oauth_states DROP CONSTRAINT {name}")
    for name, _ in reversed(COLUMNS):
        op.execute(f"ALTER TABLE public.oauth_states DROP COLUMN {name}")
