"""The stored Amazon Product Type Definition a preview or a first publish checks against (DW2 T10;
AC8, AC10, AC13).

`current_schema(uow, marketplace=..., product_type=...)` is None unless the listing is Amazon's
with a mapped product type and a current definition is stored (no definition, no check: AC10,
logged once as `ptd.missing` with the product type only). Otherwise it is the light row from one
indexed read, made ready through the validator cache: `schema_json` is read only on a cache
miss (C6). The definition is platform-global: the same row serves every tenant, and the read
carries no tenant predicate by design.
"""

from __future__ import annotations

from typing import Any

from app.adapters.amazon import constants as amazon
from app.adapters.amazon import schema_check
from app.adapters.base import ProductTypeSchema
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


async def current_schema(
    uow: Any, *, marketplace: str, product_type: str | None
) -> ProductTypeSchema | None:
    if marketplace != "amazon" or not product_type:
        return None
    ref = await uow.ptd.current(
        marketplace_id=get_settings().amazon_marketplace_id,
        product_type=product_type,
        requirements=amazon.PTD_REQUIREMENTS,
        locale=amazon.PTD_LOCALE,
    )
    if ref is None:
        log.info("ptd.missing", product_type=product_type)
        return None
    prepared: ProductTypeSchema = await schema_check.prepare(ref, uow.ptd.schema_json)
    return prepared
