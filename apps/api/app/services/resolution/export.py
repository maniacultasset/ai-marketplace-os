"""The Auto / Seller / Marketplace table (SI13 T16; P:B5, D17), pure: one row per enabled
highest-version error-map row, labelled with the lane the planner gives the code at the row's
field hint (`plan.lane_for`): `auto_repair|auto_content|ai_field|retry` -> Auto, `seller` ->
Seller, `marketplace` -> Marketplace, `staff` -> Kuinn team; a code whose lane depends on whether
the seller may still recategorise shows both (`Seller (Kuinn team once live)`). The doc is `policy_ref.doc` when it is https,
else `staff-approved` for an approved AI proposal, else a dash. The last line counts the codes no
row maps yet. Markdown escapes `|` and newlines; CSV goes through `csv.writer` and neutralises a
spreadsheet formula (a cell starting with `=`, `+`, `-`, `@`, a tab or a CR gets a leading `'`).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.paths import is_valid_field_path

from app.services.resolution.plan import PlanContext, lane_for

LABELS: Final[Mapping[str, str]] = {
    "auto_repair": "Auto",
    "auto_content": "Auto",
    "ai_field": "Auto",
    "retry": "Auto",
    "seller": "Seller",
    "marketplace": "Marketplace",
    "staff": "Kuinn team",
}
HEADER: Final = ("Marketplace", "Code", "Who fixes it", "Error code", "What it means", "Doc")
FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class ExportRow:
    marketplace: str
    code: str
    who: str
    error_code: str
    plain_text: str
    doc: str

    def cells(self) -> tuple[str, ...]:
        return (self.marketplace, self.code, self.who, self.error_code, self.plain_text, self.doc)


def _doc(policy_ref: Any) -> str:
    ref = policy_ref if isinstance(policy_ref, Mapping) else {}
    doc = ref.get("doc")
    if isinstance(doc, str) and doc.startswith("https://"):
        return doc
    if ref.get("source") == "staff-approved-ai-proposal":
        return "staff-approved"
    return "—"


def who(row: Any) -> str:
    """The lane label at the row's field hint (as the adapter builds the issue path). When a
    listing the seller may still recategorise takes another lane than one that has been live
    (CATEGORY_MISMATCH: seller, then staff), both show: `Seller (Kuinn team once live)`."""
    hint = getattr(row, "field_hint", None)
    path = hint if isinstance(hint, str) and is_valid_field_path(hint) else "listing"
    source = f"{row.marketplace}_adapter"
    live = lane_for(row.error_code, row.error_class, source, field_path=path)
    fresh = lane_for(
        row.error_code,
        row.error_class,
        source,
        field_path=path,
        ctx=PlanContext(recategorizable=True),
    )
    label = LABELS.get(live, "Kuinn team")
    if fresh == live:
        return label
    return f"{LABELS.get(fresh, 'Kuinn team')} ({label} once live)"


def rows(map_rows: Iterable[Any], unmapped_count: int = 0) -> list[ExportRow]:
    """`map_rows` carry marketplace, match_on, raw_value, version, error_code, error_class,
    plain_text, policy_ref and enabled; only the enabled highest version of each key is kept."""
    best: dict[tuple[str, str, str], Any] = {}
    for row in map_rows:
        if not row.enabled:
            continue
        key = (row.marketplace, row.match_on, row.raw_value)
        if key not in best or row.version > best[key].version:
            best[key] = row
    out = []
    for key in sorted(best):
        row = best[key]
        code = row.raw_value if row.match_on == "code" else f"category:{row.raw_value}"
        out.append(
            ExportRow(
                row.marketplace,
                code,
                who(row),
                row.error_code,
                row.plain_text,
                _doc(row.policy_ref),
            )
        )
    return out


def _md_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def to_md(table: Iterable[ExportRow], unmapped_count: int = 0) -> str:
    lines = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    lines += ["| " + " | ".join(_md_cell(c) for c in r.cells()) + " |" for r in table]
    lines.append("")
    lines.append(f"Codes seen with no row yet: {unmapped_count}")
    return "\n".join(lines) + "\n"


def safe_cell(value: str) -> str:
    return "'" + value if value.startswith(FORMULA_START) else value


def to_csv(table: Iterable[ExportRow], unmapped_count: int = 0) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for r in table:
        writer.writerow([safe_cell(c) for c in r.cells()])
    writer.writerow(["unmapped", str(unmapped_count), "", "", "", ""])
    return buffer.getvalue()
