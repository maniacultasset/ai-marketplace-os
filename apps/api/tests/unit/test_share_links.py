"""G2: the share-link rewrite (`app.services.share_links`): exact hosts only, a pure function."""

from __future__ import annotations

import pytest
from app.services.share_links import direct_url, is_drive

FILE_ID = "1AbCdEfGhIjKlMnOp_qr-st"
DIRECT = f"https://drive.google.com/uc?export=download&id={FILE_ID}"


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
        f"https://drive.google.com/file/d/{FILE_ID}/view",
        f"https://drive.google.com/file/d/{FILE_ID}",
        f"https://drive.google.com/open?id={FILE_ID}",
        f"https://drive.google.com/uc?id={FILE_ID}",
        f"https://docs.google.com/uc?id={FILE_ID}&export=download",
        f"https://DRIVE.google.com/file/d/{FILE_ID}/view",
    ],
)
def test_drive_links_become_direct(url: str) -> None:
    assert direct_url(url) == DIRECT


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com.evil.example/file/d/{FILE_ID}/view",
        f"https://evil-drive.google.com.example/file/d/{FILE_ID}/view",
        f"https://notdrive.google.com/file/d/{FILE_ID}/view",
        "https://drive.google.com/file/d/short/view",
        "https://drive.google.com/file/d/bad%2Fid12345/view",
        f"https://drive.google.com/open?id={FILE_ID}&id=other1234567",
        f"https://docs.google.com/document/d/{FILE_ID}/edit",
        f"http://drive.google.com/file/d/{FILE_ID}/view",
        f"https://user:pw@drive.google.com/file/d/{FILE_ID}/view",
        "https://cdn.example.com/p/1.jpg",
    ],
)
def test_other_links_are_unchanged(url: str) -> None:
    assert direct_url(url) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.dropbox.com/scl/fi/abc/kurta.jpg?rlkey=xyz&dl=0",
            "https://www.dropbox.com/scl/fi/abc/kurta.jpg?rlkey=xyz&dl=1",
        ),
        (
            "https://www.dropbox.com/s/abc/kurta.jpg",
            "https://www.dropbox.com/s/abc/kurta.jpg?dl=1",
        ),
        (
            "https://dropbox.com/s/abc/kurta.jpg?dl=0",
            "https://dropbox.com/s/abc/kurta.jpg?dl=1",
        ),
        (
            "https://www.dropbox.com/s/abc/kurta.jpg?dl=1",
            "https://www.dropbox.com/s/abc/kurta.jpg?dl=1",
        ),
        (
            "https://www.dropbox.com.evil.example/s/abc/kurta.jpg?dl=0",
            "https://www.dropbox.com.evil.example/s/abc/kurta.jpg?dl=0",
        ),
    ],
)
def test_dropbox_links(url: str, expected: str) -> None:
    assert direct_url(url) == expected


def test_is_drive() -> None:
    assert is_drive(f"https://drive.google.com/file/d/{FILE_ID}/view")
    assert not is_drive("https://drive.google.com.evil.example/x")
    assert not is_drive("not a url")
