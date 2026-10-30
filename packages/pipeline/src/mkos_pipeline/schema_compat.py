"""Names that moved between mkos-schemas 1.2.0 (vendored here) and backend 2.0.0."""

from __future__ import annotations

from typing import Any, Literal

try:
    from mkos_schemas.agent import AgentBlockStatus
except ImportError:  # 2.0.0: the block type lives on GenerationResult
    from mkos_schemas.jobs import AgentBlockStatus

try:
    from mkos_schemas.agent import TokenCounts
except ImportError:  # 2.0.0
    from mkos_schemas.agent import TokenUsage as TokenCounts

try:
    from mkos_schemas.rules import AttributeSpec, CategoryTemplate
except ImportError:  # 2.0.0: template types live on the jobs module
    from mkos_schemas.jobs import AttributeSpec, CategoryTemplate

try:
    from mkos_schemas.enums import (
        AgentName,
        AgentTrigger,
        BlockAgent,
        ModelPurpose,
        NodeStatus,
        RunStatus,
        TerminalState,
    )
except ImportError:  # 2.0.0 closed vocabularies split across agent/jobs
    from mkos_schemas.agent import AgentName
    from mkos_schemas.agent import Trigger as AgentTrigger
    from mkos_schemas.jobs import TerminalState

    BlockAgent = Literal["content", "keyword", "attribute", "image"]
    ModelPurpose = Literal["generation", "vision", "embedding"]
    NodeStatus = Literal["started", "ok", "failed"]
    RunStatus = Literal["running", "completed", "failed", "timeout"]


def rule_def_map(rule: object) -> dict[str, Any]:
    """1.2.0 `rule_def` is a dict; 2.0.0 is a discriminated model (`type` + `limit`)."""
    rule_def = getattr(rule, "rule_def", None)
    if rule_def is None:
        return {}
    if isinstance(rule_def, dict):
        return rule_def
    dump = getattr(rule_def, "model_dump", None)
    if callable(dump):
        dumped = dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def rule_type_of(rule: object) -> str | None:
    value = getattr(rule, "rule_type", None)
    if value:
        return str(value)
    kind = rule_def_map(rule).get("type")
    return str(kind) if kind else None


def rule_limit(rule: object, key: str) -> Any:
    dumped = rule_def_map(rule)
    if dumped.get(key) is not None:
        return dumped[key]
    return dumped.get("limit")


__all__ = [
    "AgentBlockStatus",
    "AgentName",
    "AgentTrigger",
    "AttributeSpec",
    "BlockAgent",
    "CategoryTemplate",
    "ModelPurpose",
    "NodeStatus",
    "RunStatus",
    "TerminalState",
    "TokenCounts",
    "rule_def_map",
    "rule_limit",
    "rule_type_of",
]
