"""Roles and actions (PLAN Decision 9; BACKEND-ARCHITECTURE.md §9.2): the one role x action matrix.

- `Action` is the closed set of 11 actions an `/api/v1` operation can require.
- `MATRIX` maps each role to the actions it holds: owner all; operator imports, generates,
  regenerates, edits and publishes but never approves; reviewer approves and regenerates; admin
  connects marketplaces and manages the team and settings but is not an approver (T88); every role
  reads its own tenant and its analytics.
- `Principal` is the verified caller: its `role` comes from the membership row, never the token.
- The FastAPI dependencies `require(action)` and `public()` live in `app/api/v1/deps.py` (they
  need the principal and the limiter); each is marked with its policy, and `route_policy(route)`
  reads that mark back from a route's dependency tree.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal
from uuid import UUID

from mkos_schemas.auth import Role


class Action(StrEnum):
    TENANT_READ = "tenant:read"
    PRODUCT_IMPORT = "product:import"
    LISTING_GENERATE = "listing:generate"
    LISTING_REGENERATE = "listing:regenerate"
    LISTING_EDIT = "listing:edit"
    LISTING_APPROVE = "listing:approve"
    LISTING_PUBLISH = "listing:publish"
    MARKETPLACE_CONNECT = "marketplace:connect"
    TEAM_MANAGE = "team:manage"
    SETTINGS_MANAGE = "settings:manage"
    ANALYTICS_READ = "analytics:read"


A = Action
MATRIX: Final[Mapping[Role, frozenset[Action]]] = MappingProxyType(
    {
        "owner": frozenset(Action),
        "operator": frozenset(
            {
                A.TENANT_READ,
                A.PRODUCT_IMPORT,
                A.LISTING_GENERATE,
                A.LISTING_REGENERATE,
                A.LISTING_EDIT,
                A.LISTING_PUBLISH,
                A.ANALYTICS_READ,
            }
        ),
        "reviewer": frozenset(
            {A.TENANT_READ, A.LISTING_APPROVE, A.LISTING_REGENERATE, A.ANALYTICS_READ}
        ),
        "admin": frozenset(
            {
                A.TENANT_READ,
                A.MARKETPLACE_CONNECT,
                A.TEAM_MANAGE,
                A.SETTINGS_MANAGE,
                A.ANALYTICS_READ,
            }
        ),
    }
)

PUBLIC: Final = "public"
Policy = Action | Literal["public"]
POLICY_ATTR: Final = "__mkos_policy__"


def allows(role: str, action: Action) -> bool:
    """False for an unknown role, so a stale or forged role string never grants anything."""
    held = MATRIX.get(role)  # type: ignore[call-overload]
    return held is not None and action in held


def roles_for(action: Action) -> frozenset[Role]:
    return frozenset(role for role, actions in MATRIX.items() if action in actions)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    role: Role
    email: str
    jti: str


def mark_policy[F](dependency: F, policy: Policy) -> F:
    setattr(dependency, POLICY_ATTR, policy)
    return dependency


def _calls(dependant: Any) -> Iterator[Any]:
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node.call is not None:
            yield node.call
        stack.extend(node.dependencies)


def route_policy(route: Any) -> Policy | None:
    """The one policy marked anywhere in the route's dependency tree; None when there is none.
    A route carrying two different policies is a wiring error and raises."""
    found = {
        getattr(call, POLICY_ATTR) for call in _calls(route.dependant) if hasattr(call, POLICY_ATTR)
    }
    if len(found) > 1:
        raise ValueError(f"{getattr(route, 'path', route)!s}: more than one policy {sorted(found)}")
    return next(iter(found), None)
