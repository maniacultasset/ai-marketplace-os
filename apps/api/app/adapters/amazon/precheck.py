"""The catalog and brand-restriction pre-check, pure (SI13 T4; P:A3, D14; CP-D13, CP-D18).

Inside the dry-run job the adapter searches Amazon's catalog by each sellable unit's barcode
(`searchCatalogItems`) and reads the brand restrictions of a matched ASIN
(`getListingsRestrictions`). This module turns those replies into ordinary `ErrorItem`s; it does
no I/O (`.importlinter` `pure-mapping-no-io`) and never adopts an ASIN or changes a listing.

- `brand_relation(ours, theirs)`: the marks \u2122 \u2120 \u00ae and apostrophes are dropped, both
  brands are folded (`repairs.fold`: NFKC, casefold, spaces) and split into whole tokens. Equal ->
  `same`; one token list a prefix or a subset of the other (`Zebra Co` vs `Zebra`), or the same
  letters spaced differently (`Mama Earth` vs `Mamaearth`) -> `similar`; else `other` (`Zeb` vs
  `Zebra` is `other`: tokens, never characters).
- `from_catalog`: a unit whose barcode Amazon files under an ASIN gives one `IDENTIFIER_DUPLICATE`
  item at the unit's `facts.identifier` (`variants[<sku>].facts.identifier` for a child, so the
  planner's fact key is `identifier`): `other` -> ERROR `catalog:other_brand`; `similar` -> WARNING
  `catalog:similar_brand`; `same` -> WARNING `catalog:same_brand` and the ASIN joins `matched`; no
  brand on Amazon's side -> WARNING `catalog:no_brand`. No hit -> nothing.
- `from_restrictions`: `APPROVAL_REQUIRED` or `NOT_ELIGIBLE` -> one ERROR `ACCOUNT_PERMISSION_DENIED`
  at `listing`, `restriction:<reasonCode>`, Amazon's reason text (control characters stripped)
  plus the Seller Central link on its own line, only when `allowed_link` passes it.
- `allowed_link(url)`: `https`, at most 2048 characters, host exactly `sellercentral.amazon.in` or
  `sellercentral.amazon.com`, no userinfo, no port; else None. `link_in(text)` finds such a link
  on its own line of a stored message.
The synthetic codes (`catalog:*`, `restriction:*`) never match an error-map row and their class is
never `unknown`, so the diagnosis never drafts them (CP-D18).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity
from mkos_schemas.errors import ErrorItem

from app.adapters.amazon.mapping import error, fact_path
from app.adapters.amazon.repairs import fold
from app.adapters.base import AdapterItem, AdapterListing, PrecheckResult
from app.adapters.error_map import scrub

__all__ = ["PrecheckResult", "allowed_link", "brand_relation", "from_catalog", "from_restrictions"]

BrandRelation = Literal["same", "similar", "other"]

LINK_HOSTS: Final = frozenset({"sellercentral.amazon.in", "sellercentral.amazon.com"})
LINK_MAX: Final = 2048
LINK_KEPT_MAX: Final = 500  # a longer link would not fit the 1000-character issue message
REASON_MAX: Final = 4000
MESSAGE_MAX: Final = 1000
RESTRICTED: Final = frozenset({"APPROVAL_REQUIRED", "NOT_ELIGIBLE"})
REASON_CODE_RE: Final = re.compile(r"[A-Z_]{1,64}", re.ASCII)
ASIN_RE: Final = re.compile(r"[A-Z0-9]{10}", re.ASCII)
_TOKEN_SPLIT: Final = re.compile(r"[^\w]+")
# Marks dropped before folding: NFKC turns "\u2122" into "TM" (one token "zebratm").
_BRAND_MARKS: Final = dict.fromkeys(map(ord, "\u2122\u2120\u00ae"))
# An apostrophe is read both ways: dropped ("Haldiram's" = "Haldirams") and as a separator
# ("Haldiram's" ~ "Haldiram", "L'Oreal Paris" ~ "L Oreal"). The modifier letters U+02BC and U+02BB
# are word characters, so they are named here too, with the left quote, backtick and acute accent.
_APOSTROPHE_CHARS: Final = "'\u2019\u2018\u02bc\u02bb\u0060\u00b4\uff07"
_APOSTROPHES: Final = dict.fromkeys(map(ord, _APOSTROPHE_CHARS))
_APOSTROPHE_SPACES: Final = dict.fromkeys(map(ord, _APOSTROPHE_CHARS), " ")
_RELATION_RANK: Final[dict[str, int]] = {"same": 0, "similar": 1, "other": 2}
OTHER_BRAND_MESSAGE: Final = (
    "This barcode belongs to another brand on Amazon. Check the barcode, or claim a GTIN exemption."
)
SIMILAR_BRAND_MESSAGE: Final = "Amazon lists this barcode under a similar brand name: check it."
SAME_BRAND_MESSAGE: Final = "Amazon already has this product; your offer will join it."
NO_BRAND_MESSAGE: Final = "Amazon lists this barcode with no brand: check it is your product."
RESTRICTED_MESSAGE: Final = (
    "Amazon needs your approval to list this brand or product. Request it in Seller Central."
)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SPLIT.split(fold(value.translate(_BRAND_MARKS))) if t)


def _readings(value: str) -> set[tuple[str, ...]]:
    return {_tokens(value.translate(_APOSTROPHES)), _tokens(value.translate(_APOSTROPHE_SPACES))}


def brand_relation(ours: str, theirs: str) -> BrandRelation:
    """The most lenient relation over both readings of each apostrophe: a form-only difference
    is never `other`."""
    found = [_relation(a, b) for a in _readings(ours) for b in _readings(theirs)]
    return min(found, key=_RELATION_RANK.__getitem__)


def _relation(mine: tuple[str, ...], other: tuple[str, ...]) -> BrandRelation:
    if not mine or not other:
        return "other"
    if mine == other:
        return "same"
    if "".join(mine) == "".join(other):  # spacing only ("Mama Earth" vs "Mamaearth")
        return "similar"
    short, long_ = (mine, other) if len(mine) <= len(other) else (other, mine)
    if long_[: len(short)] == short or set(short) <= set(long_):
        return "similar"
    return "other"


def barcode_of(listing: AdapterListing, unit: AdapterItem) -> str | None:
    """The unit's barcode to search by, or None (a parent, an exempt unit, no barcode)."""
    if unit.item_role == "parent":
        return None
    facts = listing.unit_facts.get(unit.product_id)
    if facts is None or facts.identifier_exempt:
        return None
    value = (facts.identifier_value or "").strip()
    return value if value.isascii() and value.isdigit() and 8 <= len(value) <= 14 else None


def identifier_type_of(listing: AdapterListing, unit: AdapterItem) -> Literal["EAN", "UPC", "GTIN"]:
    facts = listing.unit_facts.get(unit.product_id)
    kind = (facts.identifier_type or "ean").lower() if facts is not None else "ean"
    return {"upc": "UPC", "gtin": "GTIN"}.get(kind, "EAN")  # type: ignore[return-value]


def _brand_of(listing: AdapterListing) -> str | None:
    for facts in listing.unit_facts.values():
        if facts.brand_name and facts.brand_name.strip():
            return facts.brand_name.strip()
    return None


def _barcodes(entry: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    groups = entry.get("identifiers")
    for group in groups if isinstance(groups, list) else []:
        rows = group.get("identifiers") if isinstance(group, dict) else None
        for row in rows if isinstance(rows, list) else []:
            value = row.get("identifier") if isinstance(row, dict) else None
            if isinstance(value, str):
                found.add(value.strip())
    return found


def _brand_in(entry: Mapping[str, Any]) -> str | None:
    summaries = entry.get("summaries")
    for summary in summaries if isinstance(summaries, list) else []:
        brand = summary.get("brand") if isinstance(summary, dict) else None
        if isinstance(brand, str) and brand.strip():
            return brand.strip()
    return None


def _catalog_item(unit: AdapterItem, code: str, message: str, severity: Severity) -> ErrorItem:
    item = error(fact_path(unit, "identifier"), ErrorCode.IDENTIFIER_DUPLICATE, message)
    return item.model_copy(update={"severity": severity, "raw_code": code})


CatalogRelation = Literal["same", "similar", "no_brand", "other"]
_CATALOG_RANK: Final[dict[str, int]] = {"same": 0, "similar": 1, "no_brand": 2, "other": 3}


def from_catalog(
    listing: AdapterListing, units: Sequence[AdapterItem], reply_items: Any
) -> tuple[list[ErrorItem], list[tuple[str, str]], list[str]]:
    """(items, (sku, asin) of same-brand matches, ASINs of other-brand matches) for the units
    searched in one call. A barcode filed under several ASINs reports its worst relation
    (other > no brand > similar > same), whatever order Amazon lists them in; every other-brand
    ASIN is kept and a same-brand match counts only when every ASIN is the same brand. An
    unreadable entry is skipped (never raised)."""
    ours = _brand_of(listing)
    entries = reply_items if isinstance(reply_items, list) else []
    by_barcode: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("asin"), str):
            continue
        if not ASIN_RE.fullmatch(entry["asin"]):
            continue
        for barcode in _barcodes(entry):
            by_barcode.setdefault(barcode, []).append(entry)
    items: list[ErrorItem] = []
    matched: list[tuple[str, str]] = []
    others: list[str] = []
    for unit in units:
        code = barcode_of(listing, unit)
        found = by_barcode.get(code, []) if code is not None else []
        if not found:
            continue
        relations: list[tuple[CatalogRelation, str]] = []
        for entry in found:
            theirs = _brand_in(entry)
            relation: CatalogRelation = (
                "no_brand" if theirs is None else brand_relation(ours or "", theirs)
            )
            relations.append((relation, str(entry["asin"])))
        worst = max((r for r, _ in relations), key=_CATALOG_RANK.__getitem__)
        if worst == "other":
            items.append(
                _catalog_item(unit, "catalog:other_brand", OTHER_BRAND_MESSAGE, Severity.ERROR)
            )
            others += dict.fromkeys(a for r, a in relations if r == "other")
        elif worst == "no_brand":
            items.append(
                _catalog_item(unit, "catalog:no_brand", NO_BRAND_MESSAGE, Severity.WARNING)
            )
        elif worst == "similar":
            items.append(
                _catalog_item(
                    unit, "catalog:similar_brand", SIMILAR_BRAND_MESSAGE, Severity.WARNING
                )
            )
        else:
            items.append(
                _catalog_item(unit, "catalog:same_brand", SAME_BRAND_MESSAGE, Severity.WARNING)
            )
            matched.append((unit.seller_sku, relations[0][1]))
    return items, matched, others


def _clean(text: str, limit: int) -> str:
    kept = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text
    )
    return " ".join(kept.split())[:limit]


def allowed_link(url: Any) -> str | None:
    if not isinstance(url, str) or not url or len(url) > LINK_MAX or not url.isascii():
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or "@" in parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.hostname not in LINK_HOSTS or parts.netloc != parts.hostname:
        return None
    return url


def link_in(text: str | None) -> str | None:
    """The allowed Seller Central link on its own line of a stored issue message, or None."""
    for line in (text or "").splitlines():
        found = allowed_link(line.strip())
        if found is not None:
            return found
    return None


def from_restrictions(reply: Any, secrets: Iterable[str] = ()) -> list[ErrorItem]:
    """One ERROR per restricting reason of one ASIN's reply (normally one)."""
    items: list[ErrorItem] = []
    rows = reply.get("restrictions") if isinstance(reply, dict) else None
    for row in rows if isinstance(rows, list) else []:
        reasons = row.get("reasons") if isinstance(row, dict) else None
        for reason in reasons if isinstance(reasons, list) else []:
            if not isinstance(reason, dict):
                continue
            code = reason.get("reasonCode")
            if not isinstance(code, str) or code not in RESTRICTED:
                continue
            text = _clean(scrub(str(reason.get("message") or ""), secrets), REASON_MAX)
            link = None
            links = reason.get("links")
            for entry in links if isinstance(links, list) else []:
                link = allowed_link(entry.get("resource")) if isinstance(entry, dict) else None
                if link is not None:
                    break
            if link is not None and len(link) > LINK_KEPT_MAX:
                link = None
            message = RESTRICTED_MESSAGE
            if text:
                message += " Marketplace message: " + text
            message = message[: MESSAGE_MAX - (len(link) + 1 if link else 0)].rstrip()
            if link is not None:
                message += "\n" + link
            item = error("listing", ErrorCode.ACCOUNT_PERMISSION_DENIED, message)
            items.append(item.model_copy(update={"raw_code": f"restriction:{code}"}))
    return items
