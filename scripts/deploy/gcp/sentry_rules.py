#!/usr/bin/env python3
"""Sentry issue alert rules as code (`deploy.sh <env> sentry-alerts`; DEPLOY-GCP.md "Sentry alert
rules").

    sentry_rules.py --org ORG --project PROJECT --api-url https://de.sentry.io
                    --environment production [--slack-workspace ID --slack-channel '#x']
                    (--dry-run | --token-stdin)

The rules this toolkit owns are named `kuinn: ...`. Each is described before it is changed: a
rule of that name absent is created (POST), present but different in a field this file sets is
updated (PUT), the same is kept. Rules with any other name are never read past their name, never
changed and never deleted. `--dry-run` prints every rule and contacts nothing. The API token
(scopes `alerts:read`, `alerts:write`) is read from stdin, never from argv or the environment,
and never printed. Standard library only (no install needed). Exit 0 on success, 1 on an API
error, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

PREFIX: Final = "kuinn: "
TIMEOUT_S: Final = 20
SLUG: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
API_URL: Final = re.compile(r"https://([a-z0-9-]+\.)*sentry\.io")
# Alert kinds (app/core/alerts.py) a person must see again when they repeat, not only when new.
OPS_KINDS: Final = (
    "llm_provider_refused",
    "llm_credit",
    "token_refresh_failed",
    "job_dead_lettered",
)
FIRST_SEEN: Final = "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"
REGRESSION: Final = "sentry.rules.conditions.regression_event.RegressionEventCondition"
FREQUENCY: Final = "sentry.rules.conditions.event_frequency.EventFrequencyCondition"
TAGGED: Final = "sentry.rules.filters.tagged_event.TaggedEventFilter"
EMAIL: Final = "sentry.mail.actions.NotifyEmailAction"
SLACK: Final = "sentry.integrations.slack.notify_action.SlackNotifyServiceAction"
# the top-level fields compared on an existing rule (with conditions, filters and actions)
COMPARED: Final = ("actionMatch", "filterMatch", "frequency", "environment")

Transport = Callable[[str, str, str, bytes | None], tuple[int, bytes]]


def rules(
    environment: str, slack_workspace: str | None = None, slack_channel: str | None = None
) -> list[dict[str, Any]]:
    """The rules this toolkit owns, in the API's create shape."""
    actions: list[dict[str, Any]] = [
        {"id": EMAIL, "targetType": "IssueOwners", "fallthroughType": "ActiveMembers"}
    ]
    if slack_workspace and slack_channel:
        actions.append({"id": SLACK, "workspace": slack_workspace, "channel": slack_channel})

    def rule(
        name: str,
        conditions: list[dict[str, Any]],
        frequency: int,
        filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "name": PREFIX + name,
            "actionMatch": "any",
            "filterMatch": "any",
            "frequency": frequency,
            "environment": environment,
            "conditions": conditions,
            "filters": filters or [],
            "actions": [dict(a) for a in actions],
        }

    return [
        rule("new issue", [{"id": FIRST_SEEN}], 30),
        rule("regression", [{"id": REGRESSION}], 30),
        rule(
            "issue spike",
            [{"id": FREQUENCY, "value": 100, "interval": "1h", "comparisonType": "count"}],
            60,
        ),
        rule(
            "ops alert repeats",
            [
                {"id": FIRST_SEEN},
                {"id": REGRESSION},
                {"id": FREQUENCY, "value": 1, "interval": "1h", "comparisonType": "count"},
            ],
            60,
            [{"id": TAGGED, "key": "alert_kind", "match": "eq", "value": k} for k in OPS_KINDS],
        ),
    ]


def _subset(want: Any, have: Any) -> bool:
    """True when every key `want` sets has the same value in `have` (lists: same length, in order;
    numbers compare as text, as Sentry may return "100" for 100)."""
    if isinstance(want, Mapping):
        return isinstance(have, Mapping) and all(
            k in have and _subset(v, have[k]) for k, v in want.items()
        )
    if isinstance(want, list):
        return (
            isinstance(have, list)
            and len(want) == len(have)
            and all(_subset(w, h) for w, h in zip(want, have, strict=True))
        )
    return str(want) == str(have)


def differs(want: Mapping[str, Any], have: Mapping[str, Any]) -> bool:
    keys = (*COMPARED, "conditions", "filters", "actions")
    return not all(_subset(want[k], have.get(k)) for k in keys)


def urllib_transport(method: str, url: str, token: str, body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (https only)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class SentryError(Exception):
    pass


def _call(transport: Transport, method: str, url: str, token: str, doc: Any = None) -> Any:
    body = None if doc is None else json.dumps(doc).encode()
    try:
        status, raw = transport(method, url, token, body)
    except OSError as exc:
        raise SentryError(f"{method} {url}: {type(exc).__name__}") from None
    if status not in (200, 201):
        # Sentry's error body names the bad field; it never holds the token.
        raise SentryError(f"{method} {url}: HTTP {status} {raw[:300]!r}")
    return json.loads(raw or b"null")


def apply(
    wanted: Sequence[Mapping[str, Any]],
    *,
    base: str,
    token: str,
    transport: Transport,
    say: Callable[[str], None],
) -> dict[str, int]:
    """Create, update or keep each wanted rule; returns the counts."""
    existing = _call(transport, "GET", base, token)
    if not isinstance(existing, list):
        raise SentryError(f"GET {base}: not a list of rules")
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in existing:
        if isinstance(item, Mapping) and str(item.get("name", "")).startswith(PREFIX):
            by_name.setdefault(str(item["name"]), []).append(item)
    counts = {"created": 0, "updated": 0, "kept": 0}
    for rule in wanted:
        found = by_name.get(str(rule["name"]), [])
        if len(found) > 1:
            raise SentryError(f"{len(found)} rules are named {rule['name']!r}; delete the extras")
        if not found:
            _call(transport, "POST", base, token, rule)
            say(f"rule {rule['name']!r}: created")
            counts["created"] += 1
        elif differs(rule, found[0]):
            _call(transport, "PUT", f"{base}{found[0]['id']}/", token, rule)
            say(f"rule {rule['name']!r}: updated")
            counts["updated"] += 1
        else:
            say(f"rule {rule['name']!r}: kept")
            counts["kept"] += 1
    return counts


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--org", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--slack-workspace", default="")
    parser.add_argument("--slack-channel", default="")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args(argv)
    for name in ("org", "project"):
        if not SLUG.fullmatch(getattr(args, name)):
            parser.error(f"--{name} must be a Sentry slug (a-z, 0-9, _ or -)")
    if not API_URL.fullmatch(args.api_url.rstrip("/")):
        parser.error("--api-url must be https://sentry.io or a region such as https://de.sentry.io")
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", args.environment):
        parser.error("--environment must be a Sentry environment name such as production")
    if bool(args.slack_workspace) != bool(args.slack_channel):
        parser.error("--slack-workspace and --slack-channel go together")
    if args.slack_workspace and not args.slack_workspace.isdigit():
        parser.error("--slack-workspace must be the Slack integration's numeric id")
    if args.slack_channel and not re.fullmatch(r"#[a-z0-9_-]{1,80}", args.slack_channel):
        parser.error("--slack-channel must be a channel such as #kuinn-alerts")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: Any = None,
    transport: Transport = urllib_transport,
) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    wanted = rules(args.environment, args.slack_workspace or None, args.slack_channel or None)
    base = f"{args.api_url.rstrip('/')}/api/0/projects/{args.org}/{args.project}/rules/"

    def say(text: str) -> None:
        sys.stdout.write(f"sentry: {text}\n")
        sys.stdout.flush()

    if args.dry_run:
        for rule in wanted:
            say(f"rule {rule['name']!r} (dry run: create, update or keep after GET {base}):")
            sys.stdout.write(
                "".join(f"    {line}\n" for line in json.dumps(rule, indent=2).splitlines())
            )
        say(f"dry run: {len(wanted)} rules, nothing contacted")
        return 0
    token = (stdin or sys.stdin).read().strip()
    if not token or any(c.isspace() for c in token):
        sys.stderr.write("sentry: no token on stdin (one line, no spaces)\n")
        return 2
    try:
        counts = apply(wanted, base=base, token=token, transport=transport, say=say)
    except SentryError as exc:
        sys.stderr.write(f"sentry: {exc}\n")
        return 1
    say(", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
