"""The transaction boundaries services open (BACKEND-ARCHITECTURE.md §3: services own the
transaction; the `services-never-import-the-engine` import contract keeps `app.db.session` out of
`app.services`, so services reach these through the repository layer).

- `unit_of_work(tenant_id, user_id=...)`: one tenant transaction (S9).
- `identity_session()` and `set_gucs()`: the public auth flows, which learn their tenant on the
  way (CODE-PLAN S10 Q10).
"""

from __future__ import annotations

from app.db.session import identity_session, set_gucs, unit_of_work

__all__ = ["identity_session", "set_gucs", "unit_of_work"]
