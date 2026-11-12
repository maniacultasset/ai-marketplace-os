"""Client feedback 11 Nov (item 3, SKU ownership): the live SKU check and "Make one for me".

- `check_sku(principal, sku)`: whether `sku` is free in this tenant (styles and variants share
  the one `(tenant_id, sku)` unique, soft-deleted rows included), and which product holds it.
  Another tenant's SKU reads as free: the unique is per tenant.
- `suggest_sku(principal, key, path, body)`: a new SKU `<BRAND>-<CATEGORY>-<NNNN>`, idempotent on
  the Idempotency-Key (a replay answers the same SKU). The number comes from the tenant's
  `sku_counters` row for `<BRAND>-<CATEGORY>` (one atomic upsert, so concurrent requests get
  different numbers, and a number is never handed out twice). A number whose SKU the seller
  already typed by hand is skipped. Nothing else is written: the seller still saves the product.

The brand prefix is the first word of the brand name (the body's, else the tenant's), ASCII
letters and digits only, upper-cased, at most 4 characters (a word shorter than 3 takes the next
words too); `SKU` when nothing is left. The category code is the seeded category's short code, or
the first 3 letters of the slug's last word; `GEN` without a category. A category that has no
current template or is switched off (lamps while `CATEGORY_LAMPS_ENABLED` is off) is 422
`VALIDATION_FAILED` at `category`, before the key is stored: no counter is made for it. Variant SKUs keep deriving
from the base (`<base>-<COLOUR>-<SIZE>`, services/size_run.py and the variants form).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.config import get_settings
from app.core.errors import InputError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.products import ProductRepository
from app.repositories.sku_counters import SkuCounterRepository
from app.repositories.tenants import TenantProfileRepository
from app.repositories.uow import tenant_uow
from app.services.category_gate import pickable
from app.services.idempotency import idempotent

__all__ = [
    "CATEGORY_CODES",
    "SkuCheck",
    "brand_prefix",
    "category_code",
    "check_sku",
    "format_sku",
    "suggest_sku",
]

log = get_logger(__name__)
METHOD: Final = "POST"
CATEGORY_CODES: Final[Mapping[str, str]] = {
    "ethnic_wear_kurta_sets": "KUR",
    "tshirts": "TSH",
    "home_decor_lamps": "LMP",
}
NO_CATEGORY: Final = "GEN"
NO_BRAND: Final = "SKU"
PREFIX_CHARS: Final = 4
CODE_CHARS: Final = 3
DIGITS: Final = 4
# a number whose SKU is taken is skipped; this many in a row means something is wrong
MAX_SKIPS: Final = 50
_WORD: Final = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class SkuCheck:
    sku: str
    available: bool
    product_id: str | None = None
    title: str | None = None
    is_variant: bool = False
    deleted: bool = False


def _ascii_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _WORD.findall(folded)


def brand_prefix(name: str | None) -> str:
    """ "Acme Naturals" -> "ACME"; "AB Cotton" -> "ABCO"; "" or non-Latin -> "SKU" (pure)."""
    words = _ascii_words(name or "")
    if not words:
        return NO_BRAND
    prefix = words[0]
    for word in words[1:]:
        if len(prefix) >= 3:
            break
        prefix += word
    return prefix[:PREFIX_CHARS].upper()


def category_code(category: str | None) -> str:
    """The seeded short code, else the first 3 letters of the slug's last word (pure)."""
    if not category:
        return NO_CATEGORY
    known = CATEGORY_CODES.get(category)
    if known:
        return known
    words = _ascii_words(category.replace("_", " "))
    return words[-1][:CODE_CHARS].upper() if words else NO_CATEGORY


def format_sku(prefix: str, code: str, number: int) -> str:
    """`ACME-KUR-0001` (pure; more than 4 digits once past 9999)."""
    return f"{prefix}-{code}-{number:0{DIGITS}d}"


async def check_sku(principal: Principal, sku: str) -> SkuCheck:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        holder = await uow.products.holder_of_sku(sku)
    if holder is None:
        return SkuCheck(sku=sku, available=True)
    if holder.deleted:  # the unique still holds a deleted product's SKU; there is nothing to open
        return SkuCheck(sku=sku, available=False, deleted=True)
    return SkuCheck(
        sku=sku,
        available=False,
        product_id=str(holder.id),
        title=holder.title,
        is_variant=holder.parent_product_id is not None,
    )


async def suggest_sku(
    principal: Principal,
    idem_key: str,
    path: str,
    *,
    category: str | None,
    brand_name: str | None,
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    if category is not None:
        async with tenant_uow(tenant, user_id=principal.user_id) as uow:
            known = await pickable(uow, category, get_settings())
        if not known:
            raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    payload = json.dumps(
        {"category": category, "brand_name": brand_name}, sort_keys=True, ensure_ascii=False
    ).encode()

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        name = brand_name
        if not (name or "").strip():
            profile = await TenantProfileRepository(session, tenant).get_profile()
            name = None if profile is None else profile.name
        prefix, short = brand_prefix(name), category_code(category)
        counters = SkuCounterRepository(session, tenant)
        products = ProductRepository(session, tenant)
        for _ in range(MAX_SKIPS):
            sku = format_sku(prefix, short, await counters.take_next(f"{prefix}-{short}"))
            if await products.holder_of_sku(sku) is None:
                return 200, {"sku": sku}
        raise RuntimeError("no free SKU after the skip limit")

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info("sku.suggested", tenant_id=str(tenant), category=category)
    return status, body
