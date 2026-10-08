"""What a deployed (staging or production) api or worker must set since S15, for tests that boot
one: a Cloud KMS key URI of the Canon shape, https non-loopback marketplace and object-store URLs
(the OAuth redirect and S3 defaults are loopback, so a deployed boot refuses them) and S3
credentials that are not the committed MinIO pair, and (S20) the SP-API sandbox host the default
`AMAZON_USE_SANDBOX=true` requires. Values are `.invalid` and never resolve."""

from __future__ import annotations

from typing import Final

DEPLOYED_S15_ENV: Final[dict[str, str]] = {
    "KMS_KEY_URI": (
        "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/"
        "oauth-token-key"
    ),
    "DASHBOARD_BASE_URL": "https://app.example.invalid",
    "AMAZON_OAUTH_REDIRECT_URI": "https://api.example.invalid/api/v1/marketplaces/amazon/callback",
    "FLIPKART_OAUTH_REDIRECT_URI": (
        "https://api.example.invalid/api/v1/marketplaces/flipkart/callback"
    ),
    # S13b: never the committed `local_only_` MinIO pair, and an https store
    "S3_ENDPOINT_URL": "https://storage.example.invalid",
    "S3_ACCESS_KEY": "Zq9-deployed-access-key",
    "S3_SECRET_KEY": "Zq9-deployed-secret-key-not-committed",
    # S20 C17: off local the SP-API host is pinned to AMAZON_USE_SANDBOX (true by default)
    "AMAZON_SPAPI_ENDPOINT": "https://sandbox.sellingpartnerapi-eu.amazon.com",
}
