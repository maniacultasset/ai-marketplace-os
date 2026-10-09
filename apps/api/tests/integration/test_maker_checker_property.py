"""S19 T13 (DW4; AC 5; invariant 3; DATABASE.md:785, :805, :829): no maker ever approves their
own version, over random sequences of generate, edit, full and partial regeneration, import,
reject and approve by three members of one tenant.

Each step runs in its own committed transaction (its own unit of work), so the events guard's
`decided_at = now() AND decided_by_user_id = app_user_id()` test can only match that step's own
row. The oracle is a shadow contributor set kept by the test itself (reset to {maker} on
generate and full regenerate; editors added on edit; carried over by a partial regeneration
and by an import that names a parent, DATABASE.md:785), never `contributor_chain`:

- leg 1 (the API decides): when an approve reaches the maker-checker step, it is refused (item
  403) exactly when the approver is in the shadow set or is not a reviewer or owner;
- leg 2 (`approver_refusal` patched out, so every approve reaches SQL): `lv_guard`'s MKC01 gives
  the same answer, which checks the database's chain walk against the shadow on every origin.

After every step: `approved` ⇒ the approved version is the current one, and its approver is
outside the shadow set. Partial regenerations and imports have no v1 route; the test writes them
as rows (admin login), with a passed run carrying the parent's pins.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
MEMBERS = ("owner@acme.test", "operator@acme.test", "reviewer@acme.test")
APPROVERS = frozenset({"owner@acme.test", "reviewer@acme.test"})
ACTIONS = ("edit", "regenerate", "partial", "import", "reject", "approve", "approve")
EXAMPLES = int(os.environ.get("MKOS_MAKER_CHECKER_EXAMPLES", "200"))
Step = tuple[str, str]


@dataclass
class Shadow:
    listing_id: uuid.UUID
    makers: set[uuid.UUID] = field(default_factory=set)
    approvals: int = 0
    refusals: int = 0


def _steps() -> st.SearchStrategy[tuple[str, list[Step]]]:
    member = st.sampled_from(MEMBERS)
    step = st.tuples(st.sampled_from(ACTIONS), member)
    return st.tuples(member, st.lists(step, min_size=1, max_size=8))


class Machine:
    def __init__(self, world: GenWorld, loop: asyncio.AbstractEventLoop) -> None:
        self.world = world
        self.loop = loop

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def row(self, shadow: Shadow) -> dict[str, Any]:
        return self.world.listing(shadow.listing_id)

    # ---- the steps ----

    def generate(self, email: str) -> Shadow:
        from .review_support import new_product, run_job

        async def go() -> uuid.UUID:
            sku = await new_product(self.world, email)
            accepted = await make_generating(self.world, email=email, sku=sku)
            await run_job(accepted.job_id)
            return accepted.listing_id  # type: ignore[no-any-return]

        shadow = Shadow(self.run(go()), {self.world.user(email)})
        assert self.row(shadow)["status"] == "validated"
        return shadow

    def edit(self, shadow: Shadow, email: str) -> None:
        from app.core.errors import DomainError
        from app.repositories.uow import tenant_uow
        from app.services.listing_edit import edit_content
        from mkos_schemas.listing import ListingContent

        row = self.row(shadow)
        principal = self.world.principal(email)

        async def go() -> None:
            async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
                await edit_content(
                    uow,
                    principal,
                    shadow.listing_id,
                    row["current_version_id"],
                    content=ListingContent(
                        title=f"Cotton Kurta Set {uuid.uuid4().hex[:8]}",
                        bullets=["Soft cotton fabric"],
                        description="A cotton kurta set with a straight fit.",
                    ),
                    keywords=None,
                    attributes=None,
                )

        try:
            self.run(go())
        except DomainError:
            return  # not editable in this status: nothing changed
        shadow.makers.add(principal.user_id)

    def regenerate(self, shadow: Shadow, email: str) -> None:
        from app.core.errors import DomainError
        from app.repositories.uow import tenant_uow
        from app.services.generation import request_regeneration

        from .review_support import run_job

        principal = self.world.principal(email)

        async def go() -> None:
            async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
                accepted = await request_regeneration(
                    uow, principal, str(uuid.uuid4()), shadow.listing_id, None
                )
            await run_job(accepted.job_id)

        try:
            self.run(go())
        except DomainError:
            return
        assert self.row(shadow)["status"] == "validated"
        shadow.makers = {principal.user_id}

    def _row_version(self, shadow: Shadow, email: str, origin: str) -> None:
        """A partial regeneration or an import as rows (no v1 route makes them)."""
        row = self.row(shadow)
        if row["status"] not in ("validated", "needs_review", "approved", "blocked"):
            return
        conn = self.world.conn
        new_id = uuid.uuid4()
        paths = "{content}" if origin == "regeneration" else None
        with conn.transaction():
            conn.execute(
                "INSERT INTO listing_versions (id, tenant_id, listing_id, version, content_json, "
                "image_reports_json, generated_by_user_id, origin, parent_version_id, "
                "changed_paths, locale, rules_version, category_template_id) "
                "SELECT %s, tenant_id, listing_id, "
                "(SELECT max(version) + 1 FROM listing_versions WHERE listing_id = v.listing_id), "
                "jsonb_set(content_json, '{version}', to_jsonb((SELECT max(version) + 1 FROM "
                "listing_versions WHERE listing_id = v.listing_id))), image_reports_json, %s, "
                "%s, id, %s::text[], locale, rules_version, category_template_id "
                "FROM listing_versions v WHERE id = %s",
                [new_id, self.world.user(email), origin, paths, row["current_version_id"]],
            )
            conn.execute(
                "INSERT INTO validation_runs (tenant_id, listing_id, listing_version_id, "
                "marketplace, source, passed, error_report_json, attempt, rules_version, "
                "category_template_id, template_version, facts_digest, blocking_count, "
                "warning_count) SELECT tenant_id, listing_id, %s, marketplace, source, true, "
                "error_report_json, attempt, rules_version, category_template_id, "
                "template_version, facts_digest, 0, warning_count FROM validation_runs "
                "WHERE listing_version_id = %s AND source = 'rules_engine' AND passed "
                "ORDER BY created_at DESC LIMIT 1",
                [new_id, row["current_version_id"]],
            )
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                "UPDATE listings SET current_version_id = %s, approved_version_id = NULL, "
                "status = 'validated', status_reason = NULL WHERE id = %s",
                [new_id, shadow.listing_id],
            )
        # DATABASE.md:785: only a generation or a full regeneration starts a new chain, so a
        # partial regeneration and an import with a parent both carry the contributors forward
        shadow.makers.add(self.world.user(email))

    def reject(self, shadow: Shadow, email: str) -> None:
        self._decide(shadow, email, "reject")

    def approve(self, shadow: Shadow, email: str) -> None:
        self._decide(shadow, email, "approve")

    def _decide(self, shadow: Shadow, email: str, decision: str) -> None:
        from app.repositories.uow import tenant_uow
        from app.services.review import ReviewItem, approve_batch

        row = self.row(shadow)
        version = row["current_version_id"]
        principal = self.world.principal(email)
        reachable = row["status"] in ("validated", "needs_review") and (
            self.world.scalar(
                "SELECT approved_by_user_id FROM listing_versions WHERE id = %s", [version]
            )
            is None
        )
        review = ReviewItem(
            listing_id=shadow.listing_id,
            version_id=version,
            decision=decision,
            reason_code="other" if decision == "reject" else None,
            feedback="Needs another look." if decision == "reject" else None,
        )

        async def go() -> Any:
            async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
                return await approve_batch(uow, principal, [review], http_key=str(uuid.uuid4()))

        [outcome] = self.run(go()).items
        if decision == "reject" or not reachable:
            assert outcome.http_status != 403 or not reachable
            return
        maker = principal.user_id in shadow.makers or email not in APPROVERS
        code = None if outcome.error is None else outcome.error.error_code
        if maker:
            assert (outcome.http_status, code and code.value) == (403, "MAKER_CHECKER_VIOLATION")
            shadow.refusals += 1
        else:
            assert outcome.http_status == 200, (outcome.http_status, code)
            shadow.approvals += 1
            approver = self.world.scalar(
                "SELECT approved_by_user_id FROM listing_versions WHERE id = %s", [version]
            )
            assert approver == principal.user_id and approver not in shadow.makers

    def invariant(self, shadow: Shadow) -> None:
        row = self.row(shadow)
        if row["status"] == "approved":
            assert row["approved_version_id"] == row["current_version_id"]
            approver = self.world.scalar(
                "SELECT approved_by_user_id FROM listing_versions WHERE id = %s",
                [row["approved_version_id"]],
            )
            assert approver is not None and approver not in shadow.makers

    def play(self, first: str, steps: list[Step]) -> Shadow:
        shadow = self.generate(first)
        table: dict[str, Callable[[Shadow, str], None]] = {
            "edit": self.edit,
            "regenerate": self.regenerate,
            "partial": lambda s, e: self._row_version(s, e, "regeneration"),
            "import": lambda s, e: self._row_version(s, e, "import"),
            "reject": self.reject,
            "approve": self.approve,
        }
        for action, email in steps:
            table[action](shadow, email)
            self.invariant(shadow)
        return shadow


def _machine_test(world: GenWorld) -> None:
    from app.db.session import dispose_engine

    loop = asyncio.new_event_loop()
    machine = Machine(world, loop)

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(_steps())
    def run(case: tuple[str, list[Step]]) -> None:
        first, steps = case
        machine.play(first, steps)

    try:
        run()
    finally:
        loop.run_until_complete(dispose_engine())
        loop.close()


def test_maker_checker_property(gen_world: GenWorld) -> None:
    """Leg 1: the API's chain check decides."""
    _machine_test(gen_world)


def test_maker_checker_property_guard_agrees(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leg 2: the API's chain check patched out; `lv_guard` (MKC01) must agree with the shadow."""
    import app.services.review as review

    monkeypatch.setattr(review, "approver_refusal", lambda *_a: None)
    _machine_test(gen_world)
