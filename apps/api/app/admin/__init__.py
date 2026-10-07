"""The read-only staff admin at `/internal/admin` (S14b T17-T22; BUILD-SEQUENCE.md:235).

`app.main` imports this package only when `ADMIN_ENABLED=true` and `APP_ENV` is not production, so
a production process never loads sqladmin (A§14). Nothing else imports it (the
`only-main-imports-admin` import contract). Every view reads through `platform_scope()`.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqladmin import Admin
from starlette.middleware import Middleware

from app.admin.gate import StaffGate
from app.admin.session import PlatformSessionMaker
from app.core.config import Settings

TITLE = "AI Marketplace OS staff"


def mount_admin(app: FastAPI, settings: Settings) -> Admin:
    """Mount the admin on `app` at `ADMIN_BASE_URL` behind the staff gate."""
    from app.admin.views import register_views

    admin = Admin(
        app,
        session_maker=PlatformSessionMaker(),  # type: ignore[arg-type]
        base_url=settings.admin_base_url,
        title=TITLE,
        middlewares=[
            Middleware(StaffGate, app_env=settings.app_env, local_email=settings.admin_local_email)
        ],
    )
    register_views(admin)
    return admin
