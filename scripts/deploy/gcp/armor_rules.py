"""The Cloud Armor rules file of the API edge (DPP-2, ADR-0022): validate, render, plan, verify.

`deploy.sh prod edge rules|enforce` calls this helper. It never talks to Google Cloud: it reads
armor-rules.yaml and, optionally, a `gcloud compute security-policies export --file-format=json`
document, and writes the gcloud argv lines that converge the live policy to the file, one rule at a
time (`security-policies import` cannot update an existing policy's rules).

    armor_rules.py check FILE [--enforce "a b"]
    armor_rules.py plan FILE --enforce "a b" [--live LIVE.json] --out ARGV.txt   (exit 3: no change)
    armor_rules.py verify FILE --enforce "a b" --live LIVE.json                    (exit 4: differs)
    armor_rules.py summary FILE --enforce "a b"

Exit 2 on an invalid file or enforce list. Stdlib plus PyYAML (`yaml.safe_load` only).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

POLICY: Final = "kuinn-api-waf"
DEFAULT_PRIORITY: Final = 2147483647
MAX_PRIORITY: Final = DEFAULT_PRIORITY - 1
MAX_SUBEXPRESSION: Final = 1024

# Enforce order. Tiers: a group may be enforced only once every group of a lower tier is.
GROUPS: Final[tuple[str, ...]] = (
    "path",
    "rate",
    "crs-lfi",
    "crs-rfi",
    "crs-rce",
    "crs-protocolattack",
    "crs-scannerdetection",
    "crs-sqli",
    "crs-xss",
)
TIERS: Final[Mapping[str, int]] = {
    "path": 0,
    "rate": 1,
    "crs-lfi": 2,
    "crs-rfi": 2,
    "crs-rce": 2,
    "crs-protocolattack": 2,
    "crs-scannerdetection": 2,
    "crs-sqli": 3,
    "crs-xss": 3,
}
# The OWASP CRS 4.22 preconfigured rule sets (https://docs.cloud.google.com/armor/docs/waf-rules).
# methodenforcement-v422-stable is left out on purpose: it allows only GET, HEAD, POST and OPTIONS,
# and the API serves PATCH and DELETE (rule 110 is the method allow-list instead).
CRS_SETS: Final[Mapping[str, str]] = {
    "crs-lfi": "lfi-v422-stable",
    "crs-rfi": "rfi-v422-stable",
    "crs-rce": "rce-v422-stable",
    "crs-protocolattack": "protocolattack-v422-stable",
    "crs-scannerdetection": "scannerdetection-v422-stable",
    "crs-sqli": "sqli-v422-stable",
    "crs-xss": "xss-v422-stable",
}
OPT_OUT_PREFIX: Final = "owasp-crs-v042200-id"
# File action -> gcloud --action value.
ACTIONS: Final[Mapping[str, str]] = {
    "deny-403": "deny-403",
    "deny-404": "deny-404",
    "throttle": "throttle",
    "ban": "rate-based-ban",
}
RATE_ACTIONS: Final = frozenset({"throttle", "rate-based-ban"})
# API action (export) -> gcloud value.
API_ACTIONS: Final[Mapping[str, str]] = {
    "allow": "allow",
    "deny(403)": "deny-403",
    "deny(404)": "deny-404",
    "deny(429)": "deny-429",
    "deny(502)": "deny-502",
    "throttle": "throttle",
    "rate_based_ban": "rate-based-ban",
}
OPTIONS_CLAUSE: Final = "request.method != 'OPTIONS'"
RAW_PATH: Final = "request.path.matches("
UNSAFE_REGEX: Final = re.compile(r"\(\?=|\(\?!|\(\?<=|\(\?<!|\\[1-9]")
PLACEHOLDER: Final = re.compile(r"\$\{([a-z_]+)\}")
RULE_KEYS: Final = frozenset({"priority", "group", "description", "match", "action", "rate", "waf"})
RATE_KEYS: Final = frozenset(
    {"count", "interval_sec", "ban_count", "ban_interval_sec", "ban_duration_sec"}
)
WAF_KEYS: Final = frozenset({"rule_set", "sensitivity", "exclude_headers", "opt_out_rule_ids"})


class RulesError(ValueError):
    """The rules file or the enforce list is invalid."""


@dataclass(frozen=True)
class Rate:
    count: int
    interval_sec: int
    ban_count: int | None = None
    ban_interval_sec: int | None = None
    ban_duration_sec: int | None = None


@dataclass(frozen=True)
class Waf:
    rule_set: str
    sensitivity: int
    exclude_headers: tuple[str, ...] = ()
    opt_out_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    priority: int
    group: str
    description: str
    match: str
    action: str
    rate: Rate | None = None
    waf: Waf | None = None


@dataclass(frozen=True, order=True)
class Exclusion:
    target_rule_set: str
    request_header: str


@dataclass(frozen=True)
class RuleSpec:
    """One rule's desired or live state, in gcloud terms."""

    priority: int
    description: str
    expression: str | None
    action: str
    preview: bool
    rate_count: int | None = None
    rate_interval_sec: int | None = None
    ban_count: int | None = None
    ban_interval_sec: int | None = None
    ban_duration_sec: int | None = None
    conform_action: str | None = None
    exceed_action: str | None = None
    enforce_on_key: str | None = None
    exclusions: tuple[Exclusion, ...] = ()
    opt_out: tuple[str, ...] = field(default=(), compare=False)


# ---- load ----


def _int(value: object, what: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and not math.isfinite(value):
            raise RulesError(f"{what} must be a finite integer, got {value!r}")
        raise RulesError(f"{what} must be an integer, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        raise RulesError(f"{what} {value} is outside {minimum}..{maximum or 'inf'}")
    return value


def _str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulesError(f"{what} must be a non-empty string")
    return value


def _keys(item: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = set(item) - allowed
    if unknown:
        raise RulesError(f"{what}: unknown keys {sorted(unknown)}")


def _expand(match: str, patterns: Mapping[str, str], what: str) -> str:
    def replace(found: re.Match[str]) -> str:
        name = found.group(1)
        if name not in patterns:
            raise RulesError(f"{what}: unknown pattern ${{{name}}}")
        return patterns[name]

    expanded = PLACEHOLDER.sub(replace, match)
    if "${" in expanded:
        raise RulesError(f"{what}: malformed pattern reference")
    return expanded


def _rate(raw: object, action: str, what: str) -> Rate:
    if not isinstance(raw, dict):
        raise RulesError(f"{what}: {action} needs a rate mapping")
    _keys(raw, RATE_KEYS, f"{what} rate")
    ban = {k: raw.get(k) for k in ("ban_count", "ban_interval_sec", "ban_duration_sec")}
    if action == "throttle" and any(v is not None for v in ban.values()):
        raise RulesError(f"{what}: a throttle takes no ban fields")
    if action == "ban" and ban["ban_duration_sec"] is None:
        raise RulesError(f"{what}: a ban needs ban_duration_sec")
    if (ban["ban_count"] is None) != (ban["ban_interval_sec"] is None):
        raise RulesError(f"{what}: ban_count and ban_interval_sec go together")
    return Rate(
        count=_int(raw.get("count"), f"{what} rate count"),
        interval_sec=_int(raw.get("interval_sec"), f"{what} rate interval_sec"),
        ban_count=None if ban["ban_count"] is None else _int(ban["ban_count"], f"{what} ban_count"),
        ban_interval_sec=(
            None
            if ban["ban_interval_sec"] is None
            else _int(ban["ban_interval_sec"], f"{what} ban_interval_sec")
        ),
        ban_duration_sec=(
            None
            if ban["ban_duration_sec"] is None
            else _int(ban["ban_duration_sec"], f"{what} ban_duration_sec")
        ),
    )


def _waf(raw: object, group: str, what: str) -> Waf:
    if not isinstance(raw, dict):
        raise RulesError(f"{what}: a CRS rule needs a waf mapping")
    _keys(raw, WAF_KEYS, f"{what} waf")
    rule_set = _str(raw.get("rule_set"), f"{what} waf rule_set")
    if rule_set not in CRS_SETS.values():
        raise RulesError(f"{what}: rule set {rule_set!r} is not one of {sorted(CRS_SETS.values())}")
    if rule_set != CRS_SETS[group]:
        raise RulesError(f"{what}: group {group} must use {CRS_SETS[group]}")
    headers = raw.get("exclude_headers", [])
    opt_out = raw.get("opt_out_rule_ids", [])
    if not isinstance(headers, list) or not all(
        isinstance(h, str) and h == h.lower() and h for h in headers
    ):
        raise RulesError(f"{what}: exclude_headers must be lowercase header names")
    if not isinstance(opt_out, list) or not all(
        isinstance(o, str) and o.startswith(OPT_OUT_PREFIX) for o in opt_out
    ):
        raise RulesError(f"{what}: opt_out_rule_ids must start with {OPT_OUT_PREFIX}")
    return Waf(
        rule_set=rule_set,
        sensitivity=_int(raw.get("sensitivity"), f"{what} sensitivity", maximum=4),
        exclude_headers=tuple(headers),
        opt_out_rule_ids=tuple(opt_out),
    )


def _rule(raw: object, patterns: Mapping[str, str], index: int) -> Rule:
    what = f"rule #{index + 1}"
    if not isinstance(raw, dict):
        raise RulesError(f"{what} must be a mapping")
    _keys(raw, RULE_KEYS, what)
    priority = _int(raw.get("priority"), f"{what} priority", maximum=MAX_PRIORITY)
    what = f"rule {priority}"
    group = _str(raw.get("group"), f"{what} group")
    if group not in GROUPS:
        raise RulesError(f"{what}: unknown group {group!r}")
    action = _str(raw.get("action"), f"{what} action")
    if action not in ACTIONS:
        raise RulesError(f"{what}: unknown action {action!r}")
    match = _expand(_str(raw.get("match"), f"{what} match"), patterns, what)
    if UNSAFE_REGEX.search(match):
        raise RulesError(f"{what}: lookaround or backreference (not RE2)")
    is_rate = action in ("throttle", "ban")
    is_crs = group.startswith("crs-")
    if is_rate and OPTIONS_CLAUSE not in match:
        raise RulesError(f"{what}: a rate rule must skip CORS preflight ({OPTIONS_CLAUSE})")
    if (is_rate or is_crs) and RAW_PATH in match:
        raise RulesError(f"{what}: match on request.path.urlDecode(), not the raw request.path")
    if is_crs and action != "deny-403":
        raise RulesError(f"{what}: a CRS rule denies with deny-403")
    if not is_crs and raw.get("waf") is not None:
        raise RulesError(f"{what}: only crs-* groups take waf")
    if not is_rate and raw.get("rate") is not None:
        raise RulesError(f"{what}: only throttle and ban take rate")
    return Rule(
        priority=priority,
        group=group,
        description=_str(raw.get("description"), f"{what} description"),
        match=match,
        action=action,
        rate=_rate(raw.get("rate"), action, what) if is_rate else None,
        waf=_waf(raw.get("waf"), group, what) if is_crs else None,
    )


def _check_order(rules: Sequence[Rule]) -> None:
    priorities = [r.priority for r in rules]
    duplicates = sorted({p for p in priorities if priorities.count(p) > 1})
    if duplicates:
        raise RulesError(f"duplicate priorities {duplicates}")
    crs = sorted((r for r in rules if r.group.startswith("crs-")), key=lambda r: r.priority)
    if [GROUPS.index(r.group) for r in crs] != sorted(GROUPS.index(r.group) for r in crs):
        raise RulesError("CRS priorities must ascend in GROUPS order (the enforce order)")
    rate = [r.priority for r in rules if r.group == "rate"]
    if crs and rate and max(r.priority for r in crs) >= min(rate):
        raise RulesError(
            "every CRS priority must be below every rate priority (a matched rate rule allows)"
        )


def _parse(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise RulesError("the rules file must be a mapping with version and rules")
    return document


def load(path: Path) -> list[Rule]:
    """The validated rules of PATH, patterns expanded. Raises RulesError."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError(f"{path} is not UTF-8") from exc
    except OSError as exc:
        raise RulesError(f"cannot read {path}: {exc}") from exc
    document = _parse(text)
    if document.get("version") != 1:
        raise RulesError("version must be 1")
    _keys(document, frozenset({"version", "patterns", "rules"}), "the rules file")
    patterns = document.get("patterns") or {}
    if not isinstance(patterns, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in patterns.items()
    ):
        raise RulesError("patterns must map names to strings")
    for name, pattern in patterns.items():
        if len(pattern) >= MAX_SUBEXPRESSION:
            raise RulesError(f"pattern {name} is {len(pattern)} characters (limit 1023)")
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulesError("rules must be a non-empty list")
    rules = [_rule(raw, patterns, i) for i, raw in enumerate(raw_rules)]
    _check_order(rules)
    return rules


# ---- the enforce list ----


def parse_enforce(raw: str) -> list[str]:
    return raw.replace(",", " ").split()


def check_enforce(groups: Sequence[str]) -> None:
    """Raise RulesError unless GROUPS is a valid enforce list (known, unique, tiers in order)."""
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise RulesError(f"unknown groups {unknown} (known: {' '.join(GROUPS)})")
    if len(set(groups)) != len(groups):
        raise RulesError(f"duplicate groups in {list(groups)}")
    listed = set(groups)
    for group in groups:
        missing = [g for g in GROUPS if TIERS[g] < TIERS[group] and g not in listed]
        if missing:
            raise RulesError(f"{group} cannot be enforced before {' '.join(missing)}")


# ---- render ----


def _expression(rule: Rule) -> str:
    if rule.waf is None:
        return rule.match
    options = f"'sensitivity': {rule.waf.sensitivity}"
    if rule.waf.opt_out_rule_ids:
        ids = ", ".join(f"'{i}'" for i in rule.waf.opt_out_rule_ids)
        options += f", 'opt_out_rule_ids': [{ids}]"
    return f"{rule.match} && evaluatePreconfiguredWaf('{rule.waf.rule_set}', {{{options}}})"


def default_spec() -> RuleSpec:
    return RuleSpec(
        priority=DEFAULT_PRIORITY,
        description="default rule",
        expression=None,
        action="allow",
        preview=False,
    )


def render(rules: Sequence[Rule], enforce: Sequence[str]) -> list[RuleSpec]:
    """The desired per-rule state: preview unless the rule's group is enforced; plus the default."""
    specs: list[RuleSpec] = []
    for rule in sorted(rules, key=lambda r: r.priority):
        rate: dict[str, Any] = {}
        if rule.rate is not None:
            rate = {
                "rate_count": rule.rate.count,
                "rate_interval_sec": rule.rate.interval_sec,
                "ban_count": rule.rate.ban_count,
                "ban_interval_sec": rule.rate.ban_interval_sec,
                "ban_duration_sec": rule.rate.ban_duration_sec,
                "conform_action": "allow",
                "exceed_action": "deny-429",
                "enforce_on_key": "ip",
            }
        exclusions: tuple[Exclusion, ...] = ()
        opt_out: tuple[str, ...] = ()
        if rule.waf is not None:
            exclusions = tuple(
                sorted(Exclusion(rule.waf.rule_set, h) for h in rule.waf.exclude_headers)
            )
            opt_out = rule.waf.opt_out_rule_ids
        specs.append(
            RuleSpec(
                priority=rule.priority,
                description=rule.description,
                expression=_expression(rule),
                action=ACTIONS[rule.action],
                preview=rule.group not in enforce,
                exclusions=exclusions,
                opt_out=opt_out,
                **rate,
            )
        )
    specs.append(default_spec())
    return specs


def normalise_live(export: Mapping[str, Any]) -> list[RuleSpec]:
    """The same shape from an export document, ignoring the fields the server fills in."""
    specs: list[RuleSpec] = []
    for raw in export.get("rules") or []:
        priority = int(raw["priority"])
        expr = (raw.get("match") or {}).get("expr") or {}
        options = raw.get("rateLimitOptions") or {}
        threshold = options.get("rateLimitThreshold") or {}
        ban = options.get("banThreshold") or {}
        exclusions = sorted(
            Exclusion(item.get("targetRuleSet", ""), header.get("val", ""))
            for item in (raw.get("preconfiguredWafConfig") or {}).get("exclusions") or []
            for header in item.get("requestHeadersToExclude") or []
        )
        action = API_ACTIONS.get(raw.get("action", ""), raw.get("action", ""))
        rate: dict[str, Any] = {}
        if action in RATE_ACTIONS:
            rate = {
                "rate_count": _opt_int(threshold.get("count")),
                "rate_interval_sec": _opt_int(threshold.get("intervalSec")),
                "ban_count": _opt_int(ban.get("count")),
                "ban_interval_sec": _opt_int(ban.get("intervalSec")),
                "ban_duration_sec": _opt_int(options.get("banDurationSec")),
                "conform_action": API_ACTIONS.get(options.get("conformAction", ""), None),
                "exceed_action": API_ACTIONS.get(options.get("exceedAction", ""), None),
                "enforce_on_key": str(options.get("enforceOnKey", "")).lower() or None,
            }
        specs.append(
            RuleSpec(
                priority=priority,
                description=raw.get("description", ""),
                expression=expr.get("expression"),
                action=action,
                preview=bool(raw.get("preview", False)),
                exclusions=tuple(exclusions),
                **rate,
            )
        )
    return specs


def _opt_int(value: object) -> int | None:
    return None if value is None else int(str(value))


# ---- plan ----


def _base(op: str, priority: int) -> list[str]:
    return [
        "compute",
        "security-policies",
        "rules",
        op,
        str(priority),
        f"--security-policy={POLICY}",
    ]


def _flags(spec: RuleSpec) -> list[str]:
    flags: list[str] = []
    if spec.expression is not None:
        flags.append(f"--expression={spec.expression}")
    flags.append(f"--action={spec.action}")
    if spec.action in RATE_ACTIONS:
        flags += [
            f"--rate-limit-threshold-count={spec.rate_count}",
            f"--rate-limit-threshold-interval-sec={spec.rate_interval_sec}",
            f"--conform-action={spec.conform_action}",
            f"--exceed-action={spec.exceed_action}",
            f"--enforce-on-key={spec.enforce_on_key}",
        ]
        if spec.ban_count is not None:
            flags += [
                f"--ban-threshold-count={spec.ban_count}",
                f"--ban-threshold-interval-sec={spec.ban_interval_sec}",
            ]
        if spec.ban_duration_sec is not None:
            flags.append(f"--ban-duration-sec={spec.ban_duration_sec}")
    return flags


def _exclusion(op: str, priority: int, item: Exclusion) -> list[str]:
    return [
        *_base(op, priority),
        f"--target-rule-set={item.target_rule_set}",
        f"--request-header-to-exclude=op=EQUALS,val={item.request_header}",
    ]


def _create(spec: RuleSpec) -> list[list[str]]:
    argv = [*_base("create", spec.priority), *_flags(spec)]
    if spec.preview:
        argv.append("--preview")
    argv.append(f"--description={spec.description}")
    return [
        argv,
        *(_exclusion("add-preconfig-waf-exclusion", spec.priority, e) for e in spec.exclusions),
    ]


def _core(spec: RuleSpec) -> RuleSpec:
    return RuleSpec(**{**spec.__dict__, "exclusions": (), "opt_out": ()})


def plan(live: Sequence[RuleSpec], rendered: Sequence[RuleSpec]) -> list[list[str]]:
    """The gcloud argv list that converges LIVE to RENDERED: creates, then updates, then deletes."""
    have = {s.priority: s for s in live}
    want = {s.priority: s for s in rendered if s.priority != DEFAULT_PRIORITY}
    creates: list[list[str]] = []
    updates: list[list[str]] = []
    deletes: list[list[str]] = []
    for priority in sorted(want):
        spec = want[priority]
        old = have.get(priority)
        if old is None:
            creates += _create(spec)
            continue
        if (old.action in RATE_ACTIONS) != (spec.action in RATE_ACTIONS):
            updates.append(_base("delete", priority))
            updates += _create(spec)
            continue
        if _core(old) != _core(spec):
            updates.append(
                [
                    *_base("update", priority),
                    *_flags(spec),
                    "--preview" if spec.preview else "--no-preview",
                    f"--description={spec.description}",
                ]
            )
        if tuple(sorted(old.exclusions)) != tuple(sorted(spec.exclusions)):
            for item in sorted(set(old.exclusions) - set(spec.exclusions)):
                updates.append(_exclusion("remove-preconfig-waf-exclusion", priority, item))
            for item in sorted(set(spec.exclusions) - set(old.exclusions)):
                updates.append(_exclusion("add-preconfig-waf-exclusion", priority, item))
    for priority in sorted(have):
        if priority != DEFAULT_PRIORITY and priority not in want:
            deletes.append(_base("delete", priority))
    default = have.get(DEFAULT_PRIORITY)
    if default is not None and (default.action != "allow" or default.preview):
        argv = [*_base("update", DEFAULT_PRIORITY), "--action=allow"]
        if default.preview:
            argv.append("--no-preview")
        updates.append(argv)
    return creates + updates + deletes


def differences(live: Sequence[RuleSpec], rendered: Sequence[RuleSpec]) -> list[str]:
    """One line per priority whose live state differs from RENDERED."""
    lines: list[str] = []
    have = {s.priority: s for s in live}
    for spec in rendered:
        old = have.get(spec.priority)
        if spec.priority == DEFAULT_PRIORITY:
            if old is not None and (old.action != "allow" or old.preview):
                lines.append(f"{spec.priority}: the default rule is not allow")
        elif old is None:
            lines.append(f"{spec.priority}: missing")
        elif old.preview and not spec.preview:
            lines.append(f"{spec.priority}: a rule meant to be enforced is still in preview")
        elif _core(old) != _core(spec) or set(old.exclusions) != set(spec.exclusions):
            lines.append(f"{spec.priority}: differs from armor-rules.yaml")
    wanted = {s.priority for s in rendered}
    lines += [f"{p}: not in armor-rules.yaml" for p in sorted(have) if p not in wanted]
    return lines


# ---- CLI ----


def _read_live(path: str | None) -> list[RuleSpec]:
    if path is None:
        return [default_spec()]
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RulesError(f"cannot read the live export {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RulesError(f"the live export {path} is not a policy document")
    live = normalise_live(document)
    if not any(s.priority == DEFAULT_PRIORITY for s in live):
        live.append(default_spec())
    return live


def _summary(enforce: Sequence[str]) -> str:
    left = [g for g in GROUPS if g not in enforce]
    return (
        f"preview → enforce: {' '.join(enforce) or '(none)'}\n"
        f"still in preview: {' '.join(left) or '(none)'}\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="armor_rules.py")
    parser.add_argument("command", choices=("check", "plan", "verify", "summary"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--enforce", default="")
    parser.add_argument("--live")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        rules = load(args.file)
        enforce = parse_enforce(args.enforce)
        check_enforce(enforce)
        if args.command == "check":
            sys.stdout.write(
                f"rules: {len(rules)} rules OK; enforce: {' '.join(enforce) or '(none)'}\n"
            )
            return 0
        if args.command == "summary":
            sys.stdout.write(_summary(enforce))
            return 0
        rendered = render(rules, enforce)
        if args.command == "verify":
            if args.live is None:
                raise RulesError("verify needs --live")
            lines = differences(_read_live(args.live), rendered)
            if lines:
                sys.stderr.write("".join(f"rules: {line}\n" for line in lines))
                return 4
            sys.stdout.write(f"rules: {POLICY} matches armor-rules.yaml\n")
            return 0
        if args.out is None:
            raise RulesError("plan needs --out")
        steps = plan(_read_live(args.live), rendered)
        args.out.write_text("".join(shlex.join(step) + "\n" for step in steps), encoding="utf-8")
        return 0 if steps else 3
    except RulesError as exc:
        sys.stderr.write(f"rules: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
