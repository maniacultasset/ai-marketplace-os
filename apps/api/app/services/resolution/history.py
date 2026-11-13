"""The issue history read (client feedback item 20): `GET /api/v1/listings/{id}/issue-history`.
Every marketplace ERROR of one listing in plain words, who handles it, and each fix Kuinn made
with its before and after values, its reason and its outcome.

- `group_of(error_class)` (pure): the client's taxonomy (content and data, missing attribute,
  image, identifier or exemption, category mismatch, account permission, marketplace system).
- `handler_of(...)` (pure): `kuinn` for an automatic lane (an adapter repair, a content fix, an
  AI rewrite of one field, a plain retry, also of a marketplace system error), `marketplace` for
  the marketplace's lane, a map row marked `handled_outside` (a Flipkart setting Kuinn does not
  send: tax, delivery charges, stock, packages) or a marketplace system error Kuinn does not
  resend; `you` for the seller's lane; else `staff`.
- `split_message(message)` (pure): the stored message is the map row's plain text, then
  " Marketplace message: ", then the scrubbed raw text (`ErrorMapper.to_item`); an unmapped
  issue keeps only the raw text, so its cause says Kuinn's team is checking it.
- `fixes(...)` (pure): one per adapter repair (before = the refused value the issue holds, after
  = the repair's marketplace value, rendered) and one per issue Kuinn's content fix or AI rewrite
  closed (after = the value at the issue's path in the fix version that closed it: the newest
  fix version changing that path saved by the close, `after_value`). The outcome comes from the
  first send after the fix (`outcome_after`). Client item 20: a held repair (auto-fix off) is a
  suggested fix, `suggested: true` and `waiting`, and its issue is the seller's.
- `history_for(principal, listing_id)`: the read; another tenant's listing (or none) is 404.

The labels are the resolution read's (`read.labels`), so the two reads never disagree. The read
never runs the engine and never writes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from app.adapters.error_map import MARKETPLACE_PREFIX
from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.core.seller_text import seller_text, without_api_text
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.resolution.plan import ADAPTER_SOURCES, AUTOMATIC, Lane
from app.services.resolution.read import (
    MAX_ATTEMPTS,
    PREVIEW_SUFFIX,
    _numbered,
    _precheck,
    _within,
    fix_lanes,
    labels,
    recategorizable_listings,
    suggested_issues,
)

MAX_ISSUES: Final = 100
MAX_FIXES: Final = 100
MAX_PER_ATTEMPT: Final = 50
VALUE_MAX: Final = 500
GROUPS: Final[Mapping[str, str]] = {
    "missing_data": "missing_attribute",
    "invalid_value": "content_data",
    "content_error": "content_data",
    "variation_error": "content_data",
    "compliance_error": "content_data",
    "media_error": "image",
    "identifier_error": "identifier_exemption",
    "category_mismatch": "category_mismatch",
    "account_permission": "account_permission",
    "marketplace_technical": "marketplace_system",
}
LANE_HANDLERS: Final[Mapping[str, str]] = {
    **dict.fromkeys(AUTOMATIC, "kuinn"),
    "seller": "you",
    "marketplace": "marketplace",
    "staff": "staff",
}
UNMAPPED_CAUSE: Final = (
    "Kuinn does not know this marketplace message yet. Kuinn's team is checking it."
)
# Why each adapter repair rule changed a value (`adapters/amazon/repairs.py`), in plain words
REPAIR_REASONS: Final[Mapping[str, str]] = {
    "enum_fold": "The marketplace accepts only values from its own list, so Kuinn sent the "
    "matching value from that list.",
    "unit_format": "The marketplace wants the number and its unit sent apart, so Kuinn sent "
    "them that way.",
    "identifier_normalize": "The marketplace wants the barcode as digits only, so Kuinn removed "
    "the spaces and dashes.",
    "exemption_flag": "Your product has a GTIN exemption, so Kuinn sent the exemption instead "
    "of a barcode.",
    "fill_from_facts": "The marketplace needed this value and it is in your product facts, so "
    "Kuinn sent it.",
    "drop_locked": "The marketplace keeps the first value of this detail, so Kuinn stopped "
    "sending a new one.",
    "image_slot": "The marketplace refused the main photo, so Kuinn used another approved photo "
    "as the main one.",
    "fill_variant_axis": "Each variant must name its own size or colour, so Kuinn sent the "
    "variant's value.",
}
REPAIR_FALLBACK: Final = "Kuinn changed how this value is sent to the marketplace."
LEFT_OUT: Final = "Left out"
OTHER_PHOTO: Final = "Another approved photo"
CONTENT_FIX_REASON: Final = "Kuinn fixed this text to meet the marketplace's rule."
AI_FIX_REASON: Final = "Kuinn rewrote this field to meet the marketplace's rule."
ACCEPTED: Final = frozenset({"accepted", "partially_accepted", "live"})
REFUSED: Final = frozenset({"rejected", "error"})


# ---- pure parts ----


def group_of(error_class: str | None) -> str:
    return GROUPS.get(error_class or "", "unknown")


def handler_of(lane: Lane | None, *, error_class: str | None, outside: bool) -> str:
    if outside or lane == "marketplace":
        return "marketplace"
    if lane == "retry":
        return "kuinn"  # Kuinn resends on its own, also after a marketplace system error
    if error_class == "marketplace_technical":
        return "marketplace"
    if lane is None:
        return "staff"
    return LANE_HANDLERS.get(lane, "staff")


def split_message(message: str) -> tuple[str, str | None]:
    """(plain cause, the marketplace's own text)."""
    head, sep, tail = message.partition(MARKETPLACE_PREFIX)
    if not sep:
        return UNMAPPED_CAUSE, message.strip() or None
    return (head.strip() or UNMAPPED_CAUSE), (tail.strip() or None)


def _cut(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def _entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return _scalar(entry)
    value = entry.get("value")
    if isinstance(value, dict) and "value" in value:
        unit = _scalar(value.get("unit"))
        number = _scalar(value.get("value"))
        return f"{number} {unit}" if number and unit else number
    text = _scalar(value)
    unit = _scalar(entry.get("unit"))
    if text is not None:
        return f"{text} {unit}" if unit else text
    for key in sorted(entry):
        if key in ("marketplace_id", "language_tag"):
            continue
        found = _scalar(entry[key])
        if found is not None:
            return found
    return None


def render_value(value_json: Any) -> str | None:
    """A repair's marketplace value as the seller reads it: each entry's `value` (with its
    `unit`), joined; any other shape as compact JSON. Cut to 500 characters."""
    if value_json is None:
        return None
    entries = value_json if isinstance(value_json, list) else [value_json]
    parts = [p for p in (_entry(e) for e in entries) if p]
    if parts:
        return _cut(", ".join(parts), VALUE_MAX)
    return _cut(json.dumps(value_json, ensure_ascii=False, separators=(",", ":")), VALUE_MAX)


_STEP: Final = re.compile(r"([a-z0-9_]+)(?:\[(\d{1,3})\])?")


def value_at(content: Mapping[str, Any], path: str) -> Any:
    """The value at a canonical path (`content.title`, `content.bullets[2]`,
    `attributes.<key>`) of a version's content, else None."""
    node: Any = content
    for part in path.split("."):
        step = _STEP.fullmatch(part)
        if step is None or not isinstance(node, Mapping):
            return None
        node = node.get(step.group(1))
        if step.group(2) is not None:
            index = int(step.group(2))
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
    return node


def _covers(changed: str, path: str) -> bool:
    return changed == path or path.startswith((f"{changed}[", f"{changed}."))


def after_value(path: str, resolved_at: datetime, versions: Sequence[Any]) -> str | None:
    """The value at `path` in the fix version that closed the issue: the newest fix version
    changing `path` saved no later than the close (the close and the version share one
    transaction), rendered. None when no such version is kept."""
    found = [
        v
        for v in versions
        if v.created_at <= resolved_at and any(_covers(c, path) for c in v.changed_paths)
    ]
    if not found:
        return None
    newest = max(found, key=lambda v: v.created_at)
    return render_value(value_at(newest.content, path))


def outcome_after(at: datetime, submissions: Sequence[Any], *, retired: bool = False) -> str:
    """What became of a change made at `at`: the first send after it decides."""
    if retired:
        return "replaced"
    later = sorted((s for s in submissions if s.created_at > at), key=lambda s: s.created_at)
    if not later:
        return "waiting"
    outcome = later[0].outcome
    if outcome in ACCEPTED:
        return "accepted"
    if outcome in REFUSED:
        return "refused"
    if outcome == "superseded":
        return "replaced"
    return "sent"


def issue_rows(
    issues: Iterable[Any],
    lanes: Mapping[uuid.UUID, tuple[Lane, Any]],
    outside: frozenset[tuple[str, str]] = frozenset(),
    *,
    suggested: frozenset[uuid.UUID] = frozenset(),
) -> list[dict[str, Any]]:
    """The marketplace's ERROR rows, newest first. `outside` holds the (plain text, seller
    action) pairs of the map rows marked `handled_outside`; `suggested` the open issues whose
    fix waits for the seller's "Apply fixes" (client item 20)."""
    out: list[dict[str, Any]] = []
    rows = [i for i in issues if i.severity == "error" and i.source in ADAPTER_SOURCES]
    rows.sort(key=lambda r: (r.last_seen_at, r.id), reverse=True)
    for row in rows[:MAX_ISSUES]:
        if getattr(row, "marketplace_code", None) is None and MARKETPLACE_PREFIX not in (
            row.message or ""
        ):
            # B13 review: Kuinn's own preflight check (the adapter's `error()`), not the
            # marketplace's words; older rows named an API path
            cause, said = without_api_text(row.message or "") or UNMAPPED_CAUSE, None
        else:
            cause, said = split_message(row.message or "")
        lane = lanes.get(row.id, (None, None))[0]
        moved = (cause, (row.suggestion or "").strip()) in outside
        out.append(
            {
                "id": row.id,
                "field": (row.field_path or "listing")[:192],
                "error_code": row.error_code,
                "group": group_of(row.error_class),
                "handler": handler_of(lane, error_class=row.error_class, outside=moved),
                "lane": lane,
                "status": row.status,
                "marketplace_code": _cut(row.marketplace_code, 64),
                "marketplace_message": _cut(said, 1000),
                "cause": cause[:1000],
                "action": _cut(seller_text(row.suggestion), 1000),
                "value_submitted": _cut(row.value_submitted, VALUE_MAX),
                "occurrences": max(1, int(row.occurrences or 1)),
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
                "resolved_at": row.resolved_at,
                "suggested": row.status == "open" and row.id in suggested,
            }
        )
    return out


def fixes(
    *,
    repairs: Sequence[Any],
    issues: Sequence[Any],
    submissions: Sequence[Any],
    versions: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """Every fix Kuinn made, newest first. `versions` are the fix versions
    (`ResolutionRepository.fix_versions`) the content fixes' after values come from."""
    by_issue = {i.id: i for i in issues}
    out: list[dict[str, Any]] = []
    for repair in repairs:
        source = by_issue.get(repair.source_issue_id) if repair.source_issue_id else None
        # client item 20: a held repair is Kuinn's suggestion, not sent until the seller applies it
        held = bool(getattr(repair, "held", False)) and repair.retired_at is None
        if repair.rule == "drop_locked":
            after: str | None = LEFT_OUT
        elif repair.rule == "image_slot":
            after = OTHER_PHOTO
        else:
            after = render_value(repair.value_json)
        out.append(
            {
                "id": repair.id,
                "issue_id": repair.source_issue_id,
                "kind": "repair",
                "field": (repair.field_path or "listing")[:192],
                "rule": (repair.rule or None) and repair.rule[:64],
                "before": None if source is None else _cut(source.value_submitted, VALUE_MAX),
                "after": after,
                "reason": REPAIR_REASONS.get(repair.rule, REPAIR_FALLBACK),
                "outcome": (
                    "waiting"
                    if held
                    else outcome_after(
                        repair.created_at, submissions, retired=repair.retired_at is not None
                    )
                ),
                "at": repair.created_at,
                "suggested": held,
            }
        )
    for row in issues:
        if row.resolution not in ("deterministic_fix", "ai_correction") or row.resolved_at is None:
            continue
        if (row.action_taken or "").startswith("repair:"):
            continue
        ai = row.resolution == "ai_correction"
        out.append(
            {
                "id": row.id,
                "issue_id": row.id,
                "kind": "ai_correction" if ai else "content_fix",
                "field": (row.field_path or "listing")[:192],
                "rule": None,
                "before": _cut(row.value_submitted, VALUE_MAX),
                "after": after_value(row.field_path or "listing", row.resolved_at, versions),
                "reason": _cut(row.action_taken, 300)
                or (AI_FIX_REASON if ai else CONTENT_FIX_REASON),
                "outcome": outcome_after(row.resolved_at, submissions),
                "at": row.resolved_at,
            }
        )
    out.sort(key=lambda f: (f["at"], str(f["id"])), reverse=True)
    return out[:MAX_FIXES]


def counts(
    issues: Sequence[Mapping[str, Any]], fixed: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    open_rows = [i for i in issues if i["status"] == "open"]
    return {
        "fixed": sum(1 for f in fixed if not f.get("suggested")),
        "fixing": sum(1 for i in open_rows if i["handler"] == "kuinn"),
        "needs_you": sum(1 for i in open_rows if i["handler"] == "you"),
        "marketplace": sum(1 for i in open_rows if i["handler"] == "marketplace"),
        "staff": sum(1 for i in open_rows if i["handler"] == "staff"),
    }


def attempt_rows(
    *,
    jobs: Sequence[Any],
    submissions: Sequence[Any],
    total_submissions: int | None,
    versions: Mapping[uuid.UUID, Any],
    issues: Sequence[Mapping[str, Any]],
    channels: Mapping[uuid.UUID, tuple[str, uuid.UUID | None]],
    fixed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per pre-check and per send, newest first, with the ids of the issues first seen
    in its window (the pre-check's own channel and version) and the fixes made in it.
    `channels` is issue id -> (channel, version id)."""
    found = [a for a in (_precheck(j) for j in jobs) if a is not None]
    found += _numbered(submissions, total_submissions)
    found = [a for a in found if a.version_id in versions]
    found.sort(key=lambda a: (a.start, a.kind))
    out: list[dict[str, Any]] = []
    for index, attempt in enumerate(found):
        end = found[index + 1].start if index + 1 < len(found) else None
        preview = attempt.kind == "precheck"
        seen: list[uuid.UUID] = []
        for row in issues:
            channel, version_id = channels.get(row["id"], ("live", None))
            if (channel == "preview") != preview:
                continue
            if preview and version_id != attempt.version_id:
                continue
            if _within(row["first_seen_at"], attempt.start, end):
                seen.append(row["id"])
        made = [f["id"] for f in fixed if _within(f["at"], attempt.start, end)]
        out.append(
            {
                "kind": attempt.kind,
                "number": attempt.number,
                "version": versions[attempt.version_id].version,  # type: ignore[index]
                "at": attempt.at,
                "actor": attempt.actor,
                "outcome": attempt.outcome,
                "issue_ids": seen[:MAX_PER_ATTEMPT],
                "fix_ids": made[:MAX_PER_ATTEMPT],
            }
        )
    out.reverse()
    return out[:MAX_ATTEMPTS]


# ---- the read ----


async def build(uow: TenantUnitOfWork, listing: Any) -> dict[str, Any]:
    """The `IssueHistoryOut` fields of one listing (the caller checked it exists)."""
    jobs = await uow.resolution.read_jobs(listing.id)
    submissions, more = await uow.submissions.page(listing.id, after=None, limit=MAX_ATTEMPTS)
    total = await uow.submissions.count_for(listing.id) if more else None
    issues = await uow.resolution.issues_for(listing.id)
    repairs = await uow.resolution.repairs_for(listing.id)
    tasks = await uow.resolution.linked_tasks(listing.id)
    versions = await uow.resolution.versions_of(listing.id)
    suggested = await suggested_issues(uow, listing, issues, repairs, jobs)
    lanes = labels(
        issues,
        repaired={r.source_issue_id for r in repairs if r.source_issue_id is not None},
        task_kinds={t.task_id: t.kind for t in tasks},
        critical_keys=await uow.product_facts.critical_keys(),
        fixing=fix_lanes(jobs),
        recategorizable=await recategorizable_listings(uow, issues),
        locked={listing.id: frozenset(getattr(listing, "locked_paths", None) or ())},
        held=suggested,
    )
    outside = await uow.error_map.outside_texts(listing.marketplace)
    rows = issue_rows(issues, lanes, outside, suggested=suggested)
    made = fixes(
        repairs=repairs,
        issues=issues,
        submissions=submissions,
        versions=await uow.resolution.fix_versions(listing.id),
    )
    channels = {
        i.id: (
            "preview" if i.dedupe_key.endswith(PREVIEW_SUFFIX) else "live",
            i.listing_version_id,
        )
        for i in issues
    }
    return {
        "marketplace": listing.marketplace,
        "counts": counts(rows, made),
        "issues": rows,
        "fixes": made,
        "attempts": attempt_rows(
            jobs=jobs,
            submissions=submissions,
            total_submissions=total,
            versions=versions,
            issues=rows,
            channels=channels,
            fixed=made,
        ),
    }


async def history_for(principal: Principal, listing_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        return await build(uow, listing)
