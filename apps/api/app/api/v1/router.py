"""The `/api/v1` router. Every business route is mounted under it (CANON.md:33).

Include order is fixed (it is the registry's group order), so openapi.json is stable.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routers import auth, brand, imports, listings, products, tenants

router = APIRouter()
for module in (auth, tenants, brand, products, imports, listings):
    router.include_router(module.router)
