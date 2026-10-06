"""Rate-limit test helpers (CODE-PLAN S10 C6, C7; S12 reuses `reset_rate_limits`)."""

from __future__ import annotations

from typing import Any


def reset_rate_limits(admin: Any) -> None:
    """Empty `rate_limit_counters` on the admin (superuser, autocommit) test connection, so one
    test's or example's requests never count against the next one's buckets."""
    admin.execute("TRUNCATE rate_limit_counters")
