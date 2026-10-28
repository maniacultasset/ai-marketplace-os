"""A `gcloud compute security-policies export --file-format=json` document for given rule specs
(DPP-2 armor_rules.py tests and the `deploy.sh edge rules` live tests)."""

from __future__ import annotations

from types import ModuleType
from typing import Any

API_ACTION = {"deny-403": "deny(403)", "deny-404": "deny(404)", "rate-based-ban": "rate_based_ban"}


def export_of(
    ar: ModuleType, specs: list[Any], *, preview: dict[int, bool] | None = None
) -> dict[str, Any]:
    """A `security-policies export --file-format=json` document holding SPECS, with the fields the
    server fills in (kind, ruleTupleCount, preview false or absent, priority as a string)."""
    rules: list[dict[str, Any]] = []
    for spec in specs:
        rule: dict[str, Any] = {
            "kind": "compute#securityPolicyRule",
            "priority": str(spec.priority) if spec.priority % 2 else spec.priority,
            "description": spec.description,
            "action": API_ACTION.get(spec.action, spec.action),
            "ruleTupleCount": 3,
        }
        is_preview = (preview or {}).get(spec.priority, spec.preview)
        if is_preview:
            rule["preview"] = True
        elif spec.priority % 3 == 0:
            rule["preview"] = False
        if spec.expression is None:
            rule["match"] = {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": ["*"]}}
        else:
            rule["match"] = {"expr": {"expression": spec.expression}}
        if spec.rate_count is not None:
            options: dict[str, Any] = {
                "rateLimitThreshold": {
                    "count": spec.rate_count,
                    "intervalSec": spec.rate_interval_sec,
                },
                "conformAction": "allow",
                "exceedAction": "deny(429)",
                "enforceOnKey": "IP",
            }
            if spec.ban_count is not None:
                options["banThreshold"] = {
                    "count": spec.ban_count,
                    "intervalSec": spec.ban_interval_sec,
                }
            if spec.ban_duration_sec is not None:
                options["banDurationSec"] = spec.ban_duration_sec
            rule["rateLimitOptions"] = options
        if spec.exclusions:
            rule["preconfiguredWafConfig"] = {
                "exclusions": [exclusion_item(ar, e) for e in spec.exclusions]
            }
        rules.append(rule)
    return {
        "kind": "compute#securityPolicy",
        "name": "kuinn-api-waf",
        "fingerprint": "x",
        "rules": rules,
    }


def exclusion_item(ar: ModuleType, exclusion: Any) -> dict[str, Any]:
    """The export form of one Exclusion: its rule set, rule-id scope and one request field."""
    item: dict[str, Any] = {"targetRuleSet": exclusion.target_rule_set}
    if exclusion.target_rule_ids:
        item["targetRuleIds"] = list(exclusion.target_rule_ids)
    if exclusion.kind:
        entry = {"op": exclusion.op}
        if exclusion.val:
            entry["val"] = exclusion.val
        item[ar.EXCLUSION_KINDS[exclusion.kind][0]] = [entry]
    return item
