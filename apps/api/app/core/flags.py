"""The marketplace switches (S22 D1, D2; C7 "Flipkart behind a flag").

One helper reads them, so every gate (connect, callback, confirm, the token refresh, the periodic
refresh query and `open_adapter`) asks the same question the same way:

- `marketplace_enabled(marketplace, settings)`: Amazon is always on; Flipkart follows
  `FLIPKART_ENABLED` (off by default).
- `enabled_marketplaces(settings)`: the enabled ones, in register order (the periodic refresh
  filters its due-accounts query by it).
- `MarketplaceNotEnabled`: what a service raises for a disabled marketplace; the API answers it
  with the one 501 `NOT_IMPLEMENTED` envelope (`app.api.errors.marketplace_not_enabled`).
- `not_enabled_report(marketplace)`: the same refusal as the one `ErrorReport` (source `system`),
  for the worker side (`MarketplaceDisabled`).

- `publish_enabled(marketplace, settings)`: the publish kill switch, one per marketplace. Amazon
  follows `AMAZON_PUBLISH_ENABLED` (on in code, off in production until the owner says go);
  Flipkart has no switch of its own yet. Off: no write call reaches the marketplace (listing PUT,
  PATCH and the VALIDATION_PREVIEW dry run are refused in the SP-API client itself), no publish,
  retry or automatic resend is enqueued, and a queued publish job fails before it writes or
  sends anything. Reads (GET listingsItem, the status sync, OAuth connect, the seller check, the
  catalog search and the restrictions read) stay allowed.
- `PublishingPaused` / `publishing_paused_report(marketplace)`: that refusal, the same 501
  `NOT_IMPLEMENTED` shape as a switched-off marketplace with its own message.

- `account_marketplace(marketplace, settings)`: the marketplace a connected account sells on,
  `(marketplace_id, region)`. Amazon's OAuth reply names no region, so the stored region is null;
  the account sells on this deployment's `AMAZON_MARKETPLACE_ID` (A21TJRUUN4KGV, India). Flipkart
  is India only and has no marketplace id.

The message names the marketplace only: never a URL, an id or a setting value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport

if TYPE_CHECKING:
    from app.core.config import Settings

NOT_ENABLED_MESSAGE: Final = "Flipkart is not enabled"
PAUSED_MESSAGE: Final = (
    "Publishing to Amazon is paused for this workspace. Nothing was sent to Amazon."
)
PAUSED_REASON: Final = "publishing_paused"


def marketplace_enabled(marketplace: Marketplace | str, settings: Settings) -> bool:
    market = Marketplace(marketplace)
    if market is Marketplace.FLIPKART:
        return bool(settings.flipkart_enabled)
    return True


def enabled_marketplaces(settings: Settings) -> tuple[str, ...]:
    return tuple(m.value for m in Marketplace if marketplace_enabled(m, settings))


def publish_enabled(marketplace: Marketplace | str, settings: Settings) -> bool:
    """The publish kill switch (module docstring): False means no marketplace write."""
    market = Marketplace(marketplace)
    if market is Marketplace.AMAZON:
        return bool(settings.amazon_publish_enabled)
    return True


AMAZON_MARKETPLACE_REGION: Final = {
    "A21TJRUUN4KGV": "India",
    "A1F83G8C2ARO7P": "United Kingdom",
    "A1PA6795UKMFR9": "Germany",
    "ATVPDKIKX0DER": "United States",
    "A2VIGQ35RCS4UG": "United Arab Emirates",
}
"""Amazon marketplace ids (SP-API "Marketplace IDs") to the country a seller sells in."""


def account_marketplace(
    marketplace: Marketplace | str, settings: Settings
) -> tuple[str | None, str | None]:
    """`(marketplace_id, region)` for an account of `marketplace` on this deployment."""
    if Marketplace(marketplace) is Marketplace.AMAZON:
        market_id = settings.amazon_marketplace_id
        return market_id, AMAZON_MARKETPLACE_REGION.get(market_id)
    return None, "India"


def publishing_paused_message(marketplace: Marketplace | str) -> str:
    market = Marketplace(marketplace)
    return PAUSED_MESSAGE.replace("Amazon", market.value.capitalize())


def not_enabled_message(marketplace: Marketplace | str) -> str:
    market = Marketplace(marketplace)
    return NOT_ENABLED_MESSAGE.replace("Flipkart", market.value.capitalize())


class MarketplaceNotEnabled(Exception):  # noqa: N818 - a 501, not a failure of the request
    """The marketplace is switched off here; the API answers 501 NOT_IMPLEMENTED."""

    def __init__(self, marketplace: Marketplace | str) -> None:
        self.marketplace = Marketplace(marketplace)
        super().__init__(self.marketplace.value)

    def message(self) -> str:
        return not_enabled_message(self.marketplace)


class PublishingPaused(MarketplaceNotEnabled):
    """Publishing to the marketplace is paused here (`publish_enabled` is False): the same 501
    NOT_IMPLEMENTED answer with the pause's message."""

    def message(self) -> str:
        return publishing_paused_message(self.marketplace)


def not_enabled_report(
    marketplace: Marketplace | str, *, message: str | None = None
) -> ErrorReport:
    code = ErrorCode.NOT_IMPLEMENTED
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    item = ErrorItem(
        field="listing",
        error_code=code,
        message=message or not_enabled_message(marketplace),
        source=ErrorSource.SYSTEM,
        retryable=retryable,
        marketplace=Marketplace(marketplace),
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )
    return ErrorReport(status="failed", error_code=code, retryable=retryable, errors=[item])


def publishing_paused_report(marketplace: Marketplace | str) -> ErrorReport:
    """The publish kill switch's refusal as the one `ErrorReport` (NOT_IMPLEMENTED, source
    `system`, not retryable): nothing was sent."""
    return not_enabled_report(marketplace, message=publishing_paused_message(marketplace))
