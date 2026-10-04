"""The pip-audit leg of `api-lint` (EXECUTION-PLAN.md:257; ADR-0005 §6).

Audits the locked runtime and dev packages (`uv export`) plus the hash-pinned build-time packages
(`build-constraints.txt`), which the export omits. Advisories listed in
`scripts/ci/pip-audit-ignore.txt` are ignored only until their expiry date; an expired or
malformed entry fails the gate. This sends package names and versions to the OSV advisory
service, an egress exception the Tech Lead recorded (ADR-0013 §6).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IGNORE_FILE = ROOT / "scripts" / "ci" / "pip-audit-ignore.txt"
_ENTRY = re.compile(r"^(\S+)\s+(\d{4}-\d{2}-\d{2})\s+(\S.*)$")


def parse_ignore_file(text: str, today: date) -> list[str]:
    """Advisory ids to ignore. Each line is `<ID> <YYYY-MM-DD> <reason>`; `#` starts a comment."""
    ids: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY.match(line)
        if not match:
            raise ValueError(
                f"pip-audit-ignore.txt:{number}: malformed entry (<ID> <date> <reason>)"
            )
        try:
            expires = date.fromisoformat(match.group(2))
        except ValueError:
            raise ValueError(f"pip-audit-ignore.txt:{number}: malformed date") from None
        if expires < today:
            raise ValueError(f"pip-audit-ignore.txt:{number}: {match.group(1)} expired {expires}")
        ids.append(match.group(1))
    return ids


def main() -> int:
    ignored = parse_ignore_file(IGNORE_FILE.read_text(encoding="utf-8"), date.today())
    with tempfile.TemporaryDirectory() as tmp:
        exported = Path(tmp) / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--all-packages",
                "--no-emit-workspace",
                "-o",
                str(exported),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "--strict",
            "--disable-pip",
            "-s",
            "osv",
            "--timeout",
            "60",
            "-r",
            str(exported),
            "-r",
            str(ROOT / "build-constraints.txt"),
        ]
        for advisory in ignored:
            command += ["--ignore-vuln", advisory]
        return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
