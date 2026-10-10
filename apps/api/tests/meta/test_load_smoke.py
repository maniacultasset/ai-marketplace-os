"""S26 T18 (AC 7, DW2; PLAN D1, D2): `make load-smoke` (scripts/load/smoke.py, run.sh)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.services.imports.csv_file import iter_rows, parse_upload
from app.services.imports.row_model import normalize_row, validate_row

SMOKE = "scripts/load/smoke.py"
RUN = "scripts/load/run.sh"


@pytest.fixture
def smoke(repo_root: Path) -> ModuleType:
    if "load_smoke" in sys.modules:
        return sys.modules["load_smoke"]
    spec = importlib.util.spec_from_file_location("load_smoke", repo_root / SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_smoke"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("phase", ["import", "generate", "bharat"])
def test_load_smoke_rate_budget_under_100_per_min(smoke: ModuleType, phase: str) -> None:
    plan = smoke.plan_polls(phase)
    assert plan.interval_s >= 3.0  # never one poll per second per job
    assert plan.peak_per_min <= 100
    # the busiest acme minute: the import's last polls overlap the generate fan-out
    combined = smoke.plan_polls("import").peak_per_min + smoke.plan_polls("generate").peak_per_min
    assert combined <= 100


def test_load_smoke_plan_unknown_phase_refused(smoke: ModuleType) -> None:
    with pytest.raises(ValueError, match="unknown phase"):
        smoke.plan_polls("other")


def test_load_smoke_meter_counts_sliding_minute(smoke: ModuleType) -> None:
    assert smoke.peak_in_window([0, 10, 59.9, 60, 61, 200], 60.0) == 4
    assert smoke.peak_in_window([], 60.0) == 0
    meter = smoke.Meter()
    for t, status in ((0.0, 200), (1.0, 429), (2.0, 503)):
        meter.record("acme", "generate", status, t, 0.01)
    assert (meter.peak_per_min("acme"), meter.rate_limited, meter.server_errors) == (3, 1, 1)
    assert smoke.percentile([0.1, 0.2, 0.3, 0.4], 50) == 200.0
    assert smoke.percentile([], 95) is None


def test_load_smoke_csv_generator_1000_rows_valid(smoke: ModuleType) -> None:
    data = smoke.make_csv("RUN1")
    assert parse_upload(data).row_count == 1000
    skus: set[str] = set()
    for row in iter_rows(data):
        assert row.error is None
        result = validate_row(normalize_row(row.cells))
        assert result.errors == [], (row.row_number, result.errors)
        assert result.row is not None
        skus.add(row.cells["seller_sku"])
    assert len(skus) == 1000 and all(s.startswith("LOAD-RUN1-") for s in skus)
    assert smoke.make_csv("RUN2") != data  # run-unique SKUs


def _report(smoke: ModuleType, **counts: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "import_imported": 1000,
        "import_failed": 0,
        "generations_succeeded": 20,
        "dead_letters": 0,
        "server_errors": 0,
        "rate_limited": 0,
        "bharat_generation_start_s": 12.0,
        "bharat_publish_leaves_queued_s": 3.0,
        "tenant_requests_peak_per_min": {"acme": 70, "bharat": 40},
    }
    base.update(counts)
    return {"counts": base}


@pytest.mark.parametrize(
    ("counts", "missed"),
    [
        ({}, None),
        ({"import_failed": 1, "import_imported": 999}, "import"),
        ({"generations_succeeded": 19}, "generate"),
        ({"dead_letters": 1}, "dead letters"),
        ({"server_errors": 1}, "5xx"),
        ({"rate_limited": 1}, "429"),
        ({"bharat_generation_start_s": 60.5}, "60 s"),
        ({"bharat_generation_start_s": None}, "60 s"),
        ({"bharat_publish_leaves_queued_s": 31.0}, "30 s"),
        ({"tenant_requests_peak_per_min": {"acme": 101}}, "acme sent 101"),
    ],
)
def test_load_smoke_thresholds_match_ac7(
    smoke: ModuleType, counts: dict[str, Any], missed: str | None
) -> None:
    t = smoke.THRESHOLDS
    assert (t["import_rows"], t["import_failed_rows_max"]) == (1000, 0)
    assert (t["generations"], t["generations_succeeded_min"]) == (20, 20)
    assert (t["dead_letters_max"], t["server_errors_max"], t["rate_limited_max"]) == (0, 0, 0)
    assert t["bharat_generation_start_max_s"] == 60
    assert t["bharat_publish_leaves_queued_max_s"] == 30
    assert t["tenant_requests_per_min_max"] == 100
    found = smoke.failures(_report(smoke, **counts))
    if missed is None:
        assert found == []
    else:
        assert len(found) == 1 and missed in found[0], found


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000",
        "https://127.0.0.1:8000",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://example.com",
    ],
)
def test_load_smoke_local_urls_only(smoke: ModuleType, url: str) -> None:
    assert smoke.is_local("http://127.0.0.1:8123") and smoke.is_local("http://localhost:1")
    assert not smoke.is_local(url)
    assert smoke.main(["--base-url", url, "--fake-url", "http://127.0.0.1:9"]) == 2
    assert smoke.main(["--base-url", "http://127.0.0.1:9", "--fake-url", url]) == 2


def test_load_smoke_sources_local_only_no_locust(repo_root: Path) -> None:
    for name in (SMOKE, RUN):
        text = (repo_root / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from) locust", text, re.MULTILINE)
        for url in re.findall(r"https?://[^\s\"'$*|)]+", text):
            assert url.startswith(
                ("http://127.0.0.1", "http://localhost", "https://cdn.example.com")
            ), url
    run = (repo_root / RUN).read_text(encoding="utf-8")
    assert "set -euo pipefail" in run
    assert "scripts/e2e/lib/fresh_stack.sh up --profile acceptance" in run
    assert "FRESH_ENV_FILE" not in run  # production limits (D2)
    assert "trap cleanup EXIT" in run and "fresh_stack.sh down" in run
    assert '--base-url "$base_url" --fake-url "$FAKE_URL"' in run
    smoke_text = (repo_root / SMOKE).read_text(encoding="utf-8")
    assert "follow_redirects=False" in smoke_text


def test_makefile_load_smoke_target(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert (
        ".PHONY: load-smoke\nload-smoke:\n\tscripts/load/run.sh --out $(LOAD_SMOKE_OUT)" in makefile
    )
    assert "LOAD_SMOKE_OUT ?= .e2e/load-smoke.json" in makefile
    workflows = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo_root / ".github/workflows").glob("*.yml")
        if p.name != "nightly.yml"
    )
    assert "load-smoke" not in workflows  # not a required check
