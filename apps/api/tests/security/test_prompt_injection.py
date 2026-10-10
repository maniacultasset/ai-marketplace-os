"""S26 T14 (AC 6, DW4): prompt injection in product data (backend-side guards only).

Malicious titles and descriptions are data: a CSV import and a content PATCH store them verbatim
or refuse them by validation, and never change a status (only the Rules Engine and the review
flow do); the Rules Engine's outcome on an injection text equals its outcome on a neutral text of
the same length and character classes (a Hypothesis property); no other tenant's data reaches a
context, a response or a log; and only `user_instructions` selects a stub scenario.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import World2

CASES: list[dict[str, str]] = json.loads(
    (Path(__file__).parent / "fixtures/prompt_injection.json").read_text(encoding="utf-8")
)["cases"]
IDS = [c["id"] for c in CASES]


def _neutral(text: str) -> str:
    """The same length and character classes, none of the words: a letter becomes `a` (or `A`),
    a digit `0`, whitespace a space, punctuation and symbols `.`; anything else (emoji, format
    and control characters, marks) is kept as it is."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Lu":
            out.append("A")
        elif cat.startswith("L"):
            out.append("a")
        elif cat == "Nd":
            out.append("0")
        elif cat == "Zs" or ch in " \t":
            out.append(" ")
        elif cat[0] in "PS":
            out.append(".")
        else:
            out.append(ch)
    return "".join(out)


def _ruleset() -> Any:
    from tests.rules_engine_builders import rule, ruleset

    return ruleset(
        rule({"type": "max_length", "limit": 200}),
        rule({"type": "min_length", "limit": 5}),
        rule({"type": "max_length", "limit": 2000}, field="content.description"),
        rule({"type": "banned_terms", "terms": ["guaranteed", "best seller"]}, pack="brand"),
    )


def _outcome(text: str) -> tuple[str, list[tuple[str, str]]]:
    from app.services.rules_engine import evaluate
    from tests.rules_engine_builders import listing

    report = evaluate(
        listing(content={"title": text[:400] or "x", "description": text or "x"}), _ruleset()
    )
    return report.status, sorted((str(e.error_code), e.field) for e in report.errors)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_prompt_injection_rules_outcome_equals_neutral_cases(case: dict[str, str]) -> None:
    assert _outcome(case["text"]) == _outcome(_neutral(case["text"])), case["id"]


@settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    st.sampled_from([c["text"] for c in CASES]),
    st.text(min_size=0, max_size=300),
)
def test_prompt_injection_rules_outcome_equals_neutral(case: str, noise: str) -> None:
    """[prop] Any injection text, with any generated text around it, gets exactly the rules
    outcome of its neutral twin: the engine reads lengths and classes, never instructions."""
    text = f"{noise[:150]} {case} {noise[150:]}"
    assert _outcome(text) == _outcome(_neutral(text))


async def test_prompt_injection_csv_import(api: httpx.AsyncClient, world2: World2) -> None:
    """Every case as a CSV row: each product is stored with the text verbatim, or its row fails
    validation with an ErrorReport; no product, listing or job status changes beyond the import."""
    from tests.integration.imports_support import MAPPED, csv_bytes, row, run_job, seed_templates

    conn = world2.base.conn
    seed_templates(conn)
    rows = [
        row(n, product_name=case["text"][:200], description=case["text"], category=MAPPED[0])
        for n, case in enumerate(CASES, start=1)
    ]
    response = await api.post(
        "/api/v1/products/bulk",
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("injection.csv", csv_bytes(rows), "text/csv")},
    )
    assert response.status_code in (202, 422), response.text
    if response.status_code == 422:
        assert response.json()["error"]["type"] == "IMPORT_FAILED"
        return
    await run_job(uuid.UUID(response.json()["job_id"]))
    stored: dict[str, str] = dict(
        conn.execute(
            "SELECT sku, title FROM products WHERE tenant_id = %s AND sku LIKE 'SKU-%%'",
            [world2.side("acme").tenant_id],
        ).fetchall()
    )
    failed = conn.execute(
        "SELECT count(*) FROM import_rows WHERE import_id = %s AND status = 'failed'",
        [response.json()["import_id"]],
    ).fetchone()
    assert failed is not None
    assert len(stored) + int(failed[0]) == len(CASES)
    for n, case in enumerate(CASES, start=1):
        title = stored.get(f"SKU-{n:05d}")
        if title is not None:
            # stored as text: verbatim, or with its whitespace runs folded to one space
            sent = case["text"][:200]
            assert title in (sent, sent.strip(), " ".join(sent.split())), case["id"]
    listings = conn.execute(
        "SELECT count(*) FROM listings l JOIN products p ON p.id = l.product_id "
        "WHERE p.sku LIKE 'SKU-%%'"
    ).fetchone()
    assert listings is not None and listings[0] == 0  # an import never starts a listing


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_prompt_injection_content_patch(
    api: httpx.AsyncClient, world2: World2, case: dict[str, str]
) -> None:
    conn = world2.base.conn
    listing_id = world2.side("acme").ids["listing_id"]
    version_id = world2.side("acme").ids["listing_version_id"]
    before = conn.execute("SELECT status FROM listings WHERE id = %s", [listing_id]).fetchone()
    response = await api.patch(
        f"/api/v1/listings/{listing_id}/content",
        headers={**world2.headers("operator@acme.test"), "If-Match": f'"{version_id}"'},
        json={
            "content": {
                "title": case["text"][:200],
                "bullets": [case["text"][:500]],
                "description": case["text"],
            }
        },
    )
    assert response.status_code in (200, 409, 422), (case["id"], response.text)
    after = conn.execute(
        "SELECT status, approved_version_id FROM listings WHERE id = %s", [listing_id]
    ).fetchone()
    assert after is not None and before is not None
    assert after[1] is None, case["id"]  # nothing was approved
    if response.status_code == 200:
        body = response.json()
        assert body["status"] not in ("approved", "publishing", "published"), case["id"]
        stored = conn.execute(
            "SELECT content_json FROM listing_versions WHERE id = %s",
            [body["current_version_id"]],
        ).fetchone()
        assert stored is not None
        content = stored[0].get("content", stored[0])
        assert content["description"] in (case["text"], case["text"].strip()), case["id"]
    for value in world2.foreign_values("bharat"):
        assert value not in response.text


async def test_prompt_injection_no_bharat_canary_in_context_output_log_response(
    api: httpx.AsyncClient, auth_db: Any, monkeypatch: pytest.MonkeyPatch, log_lines: Any
) -> None:
    """acme generates with injection cases as instructions: the GenerationContext the pipeline
    gets, the stored version, every response and every log line hold no bharat data."""
    import mkos_pipeline
    from app.workers import generation as generation_worker
    from tests.integration.generation_support import build_gen_world, make_generating
    from tests.integration.review_support import new_product
    from tests.integration.review_support import run_job as run_generation

    seen: list[str] = []
    original = mkos_pipeline.run_listing_pipeline

    async def spy(ctx: Any, deps: Any) -> Any:
        seen.append(ctx.model_dump_json())
        return await original(ctx, deps)

    monkeypatch.setattr(generation_worker, "run_listing_pipeline", spy)
    world = await build_gen_world(auth_db, monkeypatch)
    capture = log_lines()
    bharat = world.tenant("bharat")
    bharat_skus = [
        r[0]
        for r in world.conn.execute(
            "SELECT sku FROM products WHERE tenant_id = %s", [bharat]
        ).fetchall()
    ]
    assert bharat_skus
    for case in [c for c in CASES if not c["text"].startswith("stub:")][:6]:
        sku = await new_product(world)
        accepted = await make_generating(world, sku=sku, instructions=case["text"][:2000])
        await run_generation(accepted.job_id)
        listing = await api.get(
            f"/api/v1/listings/{accepted.listing_id}",
            headers=world.headers("operator@acme.test"),
        )
        seen.append(listing.text)
        version = world.conn.execute(
            "SELECT content_json::text FROM listing_versions WHERE listing_id = %s",
            [accepted.listing_id],
        ).fetchall()
        seen.extend(str(v[0]) for v in version)
    assert len(seen) >= 12
    corpus = "\n".join(seen) + capture.text()
    assert str(bharat) not in corpus
    for sku in bharat_skus:
        assert sku not in corpus, sku


def test_prompt_injection_stub_prefix_in_title_does_not_select_scenario() -> None:
    """A `stub:` prefix in a product title or description is just text: only
    `user_instructions` selects a scenario."""
    from mkos_pipeline.graph import scenario
    from mkos_schemas.jobs import GenerationContext

    fixtures = Path(__file__).resolve().parents[4] / "packages/schemas/fixtures"
    data = json.loads((fixtures / "GenerationContext.json").read_text(encoding="utf-8"))
    for text in ("stub:blocked", "stub:timeout", "stub:failed", "stub:malformed"):
        product = dict(data.get("product") or {})
        if "title" in product:
            product["title"] = f"{text} Cotton kurta"
        ctx = GenerationContext.model_validate(
            {**data, "product": product or data.get("product"), "user_instructions": None}
        )
        assert scenario(ctx) == "ok", text
        ctx2 = GenerationContext.model_validate({**data, "user_instructions": f"please {text}"})
        assert scenario(ctx2) == "ok", text
        ctx3 = GenerationContext.model_validate({**data, "user_instructions": text})
        assert scenario(ctx3) != "ok", text
