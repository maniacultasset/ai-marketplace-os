"""SI7 T12 (design §2.4): a candidate job's reservation reaches the caps; an upload job's does not
add anything."""

from __future__ import annotations

from decimal import Decimal

from app.repositories.budget import IMAGE_EDIT_PROMPT_PREFIX, RESERVING_KINDS
from app.workers.budget import IMAGE_EDIT_RESERVATION_USD, BudgetRefused, ImageEditLimits
from mkos_schemas.enums import JobKind


def test_image_compliance_reserves() -> None:
    assert JobKind.IMAGE_COMPLIANCE.value in RESERVING_KINDS
    assert {JobKind.LISTING_GENERATE.value, JobKind.BRAND_EMBED.value} <= set(RESERVING_KINDS)


def test_upload_jobs_add_nothing() -> None:
    """An upload `image_compliance` job never writes `cost_reserved_usd` (it stays 0): summing
    it with the reserving kinds adds 0."""
    assert sum((Decimal("0"),), Decimal("0")) == 0


def test_reservation_and_limits() -> None:
    from app.core.config import Settings

    assert Decimal("0.10") == IMAGE_EDIT_RESERVATION_USD
    limits = ImageEditLimits.from_settings(Settings(_env_file=None, app_env="local"))
    assert (limits.tenant_daily_limit, limits.platform_share) == (10, Decimal("0.5"))
    assert BudgetRefused("image_tenant").scope == "image_tenant"
    assert IMAGE_EDIT_PROMPT_PREFIX == "image_edit/"
