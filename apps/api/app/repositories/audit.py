"""The audit registry (DATABASE.md §5.2 audit_log: "a CI test checks every audited entity has an
allow-list entry"). `AUDITED_ENTITIES` is the one cross-step list of `audit_log.entity_type`
values the code writes; each step that audits a new entity adds it here, and the database's
`audit_allowlist(entity_type)` (revision 0004_integrity; 0014 adds `product_type_definition`, 0017 `invitation`, 0022 `listing_repair`, 0024 `marketplace_error_proposal`) must
answer for every member."""

from __future__ import annotations

from typing import Final

AUDITED_ENTITIES: Final[frozenset[str]] = frozenset(
    {
        "brand_document",
        "brand_profile",
        "business_party",
        "category_template",
        "image",
        "image_candidate",
        "invitation",
        "job_run",
        "listing",
        "listing_issue",
        "listing_repair",
        "marketplace_account",
        "marketplace_error_map",
        "marketplace_error_proposal",
        "membership",
        "product",
        "product_type_definition",
        "rules",
        "tenant",
        "user",
    }
)
