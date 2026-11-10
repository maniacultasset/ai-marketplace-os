"""The `/api/v1` router. Every business route is mounted under it (CANON.md:33).

Include order is fixed (it is the registry's group order), so openapi.json is stable.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routers import (
    admin,
    analytics,
    auth,
    brand,
    catalog,
    category_suggestion,
    column_maps,
    google,
    image_candidates,
    images,
    imports,
    invites,
    issues,
    jobs,
    listings,
    marketplaces,
    members,
    products,
    rules,
    signup,
    size_chart_read,
    size_run,
    tasks,
    tenants,
)

router = APIRouter()
for module in (
    auth,
    google,
    members,
    invites,
    tenants,
    brand,
    products,
    imports,
    listings,
    marketplaces,
    images,
    image_candidates,
    rules,
    jobs,
    issues,
    tasks,
    analytics,
    signup,
    admin,
    size_chart_read,
    catalog,
    category_suggestion,
    column_maps,
    size_run,
):
    router.include_router(module.router)
