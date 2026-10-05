"""Round 4 re-check of the seam secret walk: escaped JSON reaches every walked model."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mkos_schemas.base import find_seam_secret_key
from mkos_schemas.jobs import BrandChunkDTO
from pydantic import ValidationError

_SECRET = "sk-ROUND4-FAKE-SECRET"
_INNER = {"access_token": _SECRET}
_CASES = [
    "body=" + json.dumps(json.dumps(json.dumps(_INNER))),
    "retry " + json.dumps(json.dumps(_INNER)) + " later",
    "cell: " + json.dumps(_INNER).replace('"', '\\"') + " (quoted log line)",
    json.dumps([{"note": "x " + json.dumps(json.dumps(_INNER))}]),
]


def _chunk(text: str) -> dict[str, Any]:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "type": "tone_rule",
        "text": text,
        "similarity": 0.5,
        "source_type": "brand_document",
        "source_id": "33333333-3333-3333-3333-333333333333",
    }


@pytest.mark.parametrize("text", _CASES)
def test_brand_chunk_rejects_escaped_secret_json(text: str) -> None:
    assert find_seam_secret_key(text) == "access_token"
    with pytest.raises(ValidationError, match="forbidden seam key") as exc:
        BrandChunkDTO.model_validate(_chunk(text))
    assert _SECRET not in str(exc.value)


def test_brand_chunk_keeps_quoted_prose() -> None:
    text = 'Say "gentle" and "{brand}" in [the] headline; avoid \\"miracle\\".'
    assert BrandChunkDTO.model_validate(_chunk(text)).text == text
