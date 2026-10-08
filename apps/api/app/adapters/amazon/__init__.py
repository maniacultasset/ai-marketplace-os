"""The Amazon SP-API adapter (S20): Listings Items API 2021-08-01.

- `constants`: the wire names (believed from the public SP-API docs; DW1 confirms them).
- `mapping`: canonical listing -> Listings Items payloads, the template checks and `preview()`.
  Pure: no HTTP, socket or database.
- `endpoint`: the SP-API host pinned to `AMAZON_USE_SANDBOX` (stdlib only).
- `client`: the one HTTP client (httpx), with the limiter, 429 backoff and call logging.
- `adapter`: `AmazonAdapter`, the `MarketplaceAdapter` over the client.

Only `app.adapters` and `app.workers` import `client` and `adapter` (.importlinter).
"""
