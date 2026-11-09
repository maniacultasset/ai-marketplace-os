"""The stub `map_columns` (SI14 T5): deterministic, no clock, no I/O.

Each request header maps to the first dictionary key (not taken) that is a substring of the
header, normalised to lower case with spaces as `_`, at 0.9; else to null at 0. Header markers:
`lowconf` gives 0.79, `outside` answers a key not in the dictionary, `boom` raises. One record
(agent `attribute`, trigger `generate`, prompt `map_columns/stub-1`). `LAST_REQUEST` keeps the last
request so tests can assert the backend never sends sample cells.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.columns import ColumnMapping, ColumnMapRequest, ColumnMapResult
from mkos_schemas.jobs import ModelCredentials

PROMPT_VERSION: Final = "map_columns/stub-1"
STUB_CONFIDENCE: Final = 0.9
LOW_CONFIDENCE: Final = 0.79
STUB_COST: Final = Decimal("0.0005")
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
OUTSIDE_KEY: Final = "not_in_dictionary"

LAST_REQUEST: ColumnMapRequest | None = None


def _normal(header: str) -> str:
    return "_".join(header.strip().lower().split())


def _mapping(header: str, keys: list[str]) -> ColumnMapping:
    name = _normal(header)
    if "outside" in name:
        return ColumnMapping(key=OUTSIDE_KEY, confidence=STUB_CONFIDENCE)
    for key in keys:
        if key in name:
            confidence = LOW_CONFIDENCE if "lowconf" in name else STUB_CONFIDENCE
            return ColumnMapping(key=key, confidence=confidence)
    return ColumnMapping(key=None, confidence=0.0)


async def map_columns(request: ColumnMapRequest, credentials: ModelCredentials) -> ColumnMapResult:
    global LAST_REQUEST
    LAST_REQUEST = request
    headers = [column.header for column in request.headers]
    if any("boom" in _normal(header) for header in headers):
        raise RuntimeError("stub map_columns failure")
    taken = set(request.taken_keys)
    keys = [entry.key for entry in request.dictionary if entry.key not in taken]
    digest = hashlib.sha256(f"{PROMPT_VERSION}{'|'.join(headers)}".encode()).hexdigest()
    record = AgentRunRecord(
        id=uuid5(request.tenant_id, f"map_columns:{request.request_id}:{digest}"),
        tenant_id=request.tenant_id,
        product_id=None,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="attribute",
        trigger="generate",
        attempt=1,
        model_used=credentials.model,
        prompt_version=PROMPT_VERSION,
        tokens=TokenUsage(),
        cost_usd=STUB_COST,
        status="completed",
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )
    return ColumnMapResult(
        status="ok",
        mappings={header: _mapping(header, keys) for header in headers},
        confidence_version="stub-1",
        agent_runs=[record],
    )
