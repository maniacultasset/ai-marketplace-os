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


def marketplace_enabled(marketplace: Marketplace | str, settings: Settings) -> bool:
    market = Marketplace(marketplace)
    if market is Marketplace.FLIPKART:
        return bool(settings.flipkart_enabled)
    return True


def enabled_marketplaces(settings: Settings) -> tuple[str, ...]:
    return tuple(m.value for m in Marketplace if marketplace_enabled(m, settings))


def not_enabled_message(marketplace: Marketplace | str) -> str:
    market = Marketplace(marketplace)
    return NOT_ENABLED_MESSAGE.replace("Flipkart", market.value.capitalize())


class MarketplaceNotEnabled(Exception):  # noqa: N818 - a 501, not a failure of the request
    """The marketplace is switched off here; the API answers 501 NOT_IMPLEMENTED."""

    def __init__(self, marketplace: Marketplace | str) -> None:
        self.marketplace = Marketplace(marketplace)
        super().__init__(self.marketplace.value)


def not_enabled_report(marketplace: Marketplace | str) -> ErrorReport:
    code = ErrorCode.NOT_IMPLEMENTED
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    item = ErrorItem(
        field="listing",
        error_code=code,
        message=not_enabled_message(marketplace),
        source=ErrorSource.SYSTEM,
        retryable=retryable,
        marketplace=Marketplace(marketplace),
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )
    return ErrorReport(status="failed", error_code=code, retryable=retryable, errors=[item])
