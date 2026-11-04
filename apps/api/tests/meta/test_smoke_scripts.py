"""DW2 T11: the manual PTD sandbox smoke never prints a credential."""

from __future__ import annotations

import os
import re
from pathlib import Path

SCRIPT = Path("scripts/smoke/amazon_ptd_sandbox.sh")


def test_ptd_smoke_script_is_safe(repo_root: Path) -> None:
    path = repo_root / SCRIPT
    text = path.read_text(encoding="utf-8")
    assert os.access(path, os.X_OK)
    assert "set -euo pipefail" in text.splitlines()[:10]
    assert "set -x" not in text and "xtrace" not in text
    assert not re.search(r"echo[^\n]*\$\{?AMAZON", text)
    assert not re.search(r"printf[^\n]*\$\{?AMAZON", text)
    # the env file is only sourced, inside a subshell, never cat'ed or copied
    assert '. "$env_file"' in text
    assert not re.search(r"(cat|cp|tee)\s+[^\n]*env_file", text)
    assert "trap 'rm -rf \"$tmp\"' EXIT" in text
    assert "--dry-run" in text and "--save-schema" in text
    assert "Atza" in text and "X-Amz-" in text  # the output scan
