"""The `/api/v1` router. Every business route is mounted under it (CANON.md:33).

Include order is fixed (it is the registry's group order), so openapi.json is stable.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routers import (
    analytics,
    auth,
    brand,
    images,
    imports,
    jobs,
    listings,
    marketplaces,
    products,
    rules,
    tenants,
)

router = APIRouter()
for module in (
    auth,
    tenants,
    brand,
    products,
    imports,
    listings,
    marketplaces,
    images,
    rules,
    jobs,
    analytics,
):
    router.include_router(module.router)
