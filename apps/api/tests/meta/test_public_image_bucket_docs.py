"""SI1 fix round 1 (security review): the owner step for the public image bucket names the exact
IAM roles. `allUsers` gets `roles/storage.legacyObjectReader` (object get only), never
`roles/storage.objectViewer`, whose `storage.objects.list` would make every tenant's public
names listable. The storage identity's role on the public bucket is named, and the check step
proves anonymous listing is refused while a known object is served."""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = (
    "docs/adr/0026-public-image-copies.md",
    "docs/canon/ENV.md",
    "docs/runbooks/DEPLOY-GCP.md",
)
GRANT = (
    "gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=allUsers "
    "--role=roles/storage.legacyObjectReader"
)


@pytest.mark.parametrize("doc", DOCS)
def test_public_read_role_is_named(repo_root: Path, doc: str) -> None:
    text = " ".join((repo_root / doc).read_text(encoding="utf-8").split())
    assert GRANT in text, doc
    assert "never grant `roles/storage.objectviewer`" in text.lower(), doc
    for line in text.split(". "):
        if "allUsers" in line and "objectViewer" in line:
            assert "never" in line.lower(), line


@pytest.mark.parametrize(
    "doc", ("docs/adr/0026-public-image-copies.md", "docs/runbooks/DEPLOY-GCP.md")
)
def test_storage_identity_role_and_check_step(repo_root: Path, doc: str) -> None:
    text = " ".join((repo_root / doc).read_text(encoding="utf-8").split())
    assert "--role=roles/storage.objectUser" in text, doc
    assert (
        "curl -s -o /dev/null -w '%{http_code}' https://storage.googleapis.com/storage/v1/b/<public-bucket>/o"
        in text
    )
    assert "401 or 403" in text and "200" in text
