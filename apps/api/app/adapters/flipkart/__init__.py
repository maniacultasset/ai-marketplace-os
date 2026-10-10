"""The Flipkart Seller API adapter (S22): listings v3 (believed; DW1 confirms the wire shape).

- `constants`: the wire names, chunk size and rates (believed, DW1).
- `mapping`: canonical listing -> Flipkart listing payloads, the template checks, family
  payloads (children only, grouped by colour) and `preview()`. Pure: no HTTP, socket or database.
- `endpoint`: the API base and OAuth URLs pinned off local (stdlib only).
- `client`: the one HTTP client (httpx), with the limiter, 429 backoff and call logging.
- `adapter`: `FlipkartAdapter`, the `MarketplaceAdapter` over the client.

Only `app.adapters` and `app.workers` import `client` and `adapter` (.importlinter).
"""
