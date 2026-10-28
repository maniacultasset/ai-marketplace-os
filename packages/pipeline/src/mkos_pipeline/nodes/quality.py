from typing import Protocol

from mkos_schemas.errors import ErrorItem
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import RuleSet


class ClaimCheckResult(Protocol):
    listing: CanonicalListing
    claims: list[object]
    removed: list[ErrorItem]


class _NoOpResult:
    def __init__(self, listing: CanonicalListing) -> None:
        self.listing = listing
        self.claims: list[object] = []
        self.removed: list[ErrorItem] = []


def run_claim_check(draft: CanonicalListing, product: dict, rules: RuleSet) -> ClaimCheckResult:
    """Protocol hook for Developer 2 (D2-21). No-op until that task lands."""
    del product, rules
    return _NoOpResult(draft)
