"""SI13 T11 (AC13; P:B1): proposals never act. No module on the issue, planner, task, seller or HTTP
path imports the proposal store or reads `marketplace_error_proposals`; only the diagnosis job,
the scheduler, the staff CLI, the platform loader and the ops page do."""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
NEEDLES = ("error_proposals", "marketplace_error_proposals", "ErrorProposalRepository")
GUARDED = (
    "services/issues.py",
    "services/issue_questions.py",
    "services/resolution/plan.py",
    "services/resolution/run.py",
    "services/resolution/read.py",
    "services/remote_preview.py",
    "services/status_sync_outcome.py",
    "services/tasks.py",
    "services/review.py",
)


def _guarded_files() -> list[Path]:
    found = [APP / rel for rel in GUARDED if (APP / rel).exists()]
    found += sorted((APP / "api").rglob("*.py"))
    found += sorted(p for p in (APP / "services").glob("issues*.py"))
    return found


def test_no_seller_or_routing_module_reads_proposals() -> None:
    files = _guarded_files()
    assert len(files) > 20, "is the app tree where this test expects it?"
    for path in files:
        text = path.read_text("utf-8")
        for needle in NEEDLES:
            assert needle not in text, f"{path.relative_to(APP)} mentions {needle}"


def test_the_store_is_reached_only_from_staff_and_job_modules() -> None:
    allowed = {
        "repositories/error_proposals.py",
        "workers/error_diagnosis.py",
        "workers/periodic.py",
        "workers/embed_jobs.py",
        "services/job_hooks.py",
        "cli.py",
        "db/platform_loads.py",
        "db/models/platform.py",
        "admin/ops_views.py",
        "repositories/error_catalog.py",
    }
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP))
        if rel in allowed:
            continue
        text = path.read_text("utf-8")
        assert "error_proposals import" not in text and "ErrorProposalRepository" not in text, rel
