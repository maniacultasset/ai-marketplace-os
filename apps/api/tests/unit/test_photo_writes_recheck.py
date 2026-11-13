"""Client Chrome pass B14: a colour read from the photo is not "missing" in the checks.

Prod, 9 Nov (listing 372afc4b, DEMO-KURTA-IND-01, job a5079d28): the generation's photo read
wrote 7 unconfirmed `ai` facts in the persist, colour Navy among them. The version and its
re-check had been built from the facts read before the photo step, so the re-check reported
`attributes.color` MISSING_ATTRIBUTE (an Inbox issue and a question), while the persist stamped
the re-read digest on it and the marketplace preview overlaid the re-read facts and sent
"color: Navy". The worker now builds the version and the re-check from `with_photo_writes`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.services.photo_facts import PhotoDecision, PhotoWrite, with_photo_writes
from app.services.rules_engine import evaluate
from app.services.seller_facts import seller_overlay
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem
from tests.rules_engine_builders import ruleset
from tests.unit.test_seller_overlay import fact, facts, kurta_template, listing

RUN = uuid.uuid4()
IMAGE = uuid.uuid4()
NAVY = PhotoDecision(RUN, writes=(PhotoWrite("color", "Navy", 0.93, IMAGE),))


def _missing(report_errors: Sequence[ErrorItem], key: str) -> bool:
    return any(
        e.field == f"attributes.{key}" and e.error_code is ErrorCode.MISSING_ATTRIBUTE
        for e in report_errors
    )


def test_pre_photo_facts_report_the_colour_missing() -> None:
    template = kurta_template()
    pre = facts({"fabric_type": "Cotton"})
    version = seller_overlay(listing(fabric_type="Cotton"), pre, template)
    report = evaluate(version, ruleset(), template=template, product=None)
    assert _missing(report.errors, "color")


def test_the_photo_write_reaches_the_version_and_its_recheck() -> None:
    template = kurta_template()
    pre = facts({"fabric_type": "Cotton"})
    after = with_photo_writes(pre, NAVY)
    version = seller_overlay(listing(fabric_type="Cotton"), after, template)
    assert version.attributes["color"] == "Navy"
    report = evaluate(version, ruleset(), template=template, product=None)
    assert not _missing(report.errors, "color")
    stored = after.attributes["color"]
    assert (stored.source, stored.confirmed_at, stored.agent_run_id) == ("ai", None, str(RUN))


def test_a_seller_value_is_never_replaced() -> None:
    pre = facts({"color": "Mustard"})
    assert with_photo_writes(pre, NAVY).attributes["color"] == fact("color", "Mustard")


def test_an_unconfirmed_photo_value_is_replaced_and_a_failed_read_changes_nothing() -> None:
    old = facts()
    old = old.model_copy(
        update={"attributes": {"color": fact("color", "Red").model_copy(update={"source": "ai"})}}
    )
    assert with_photo_writes(old, NAVY).attributes["color"].value == "Navy"
    assert with_photo_writes(old, PhotoDecision(None, writes=NAVY.writes)) is old
    assert with_photo_writes(old, PhotoDecision(RUN)) is old
