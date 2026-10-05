"""Hash-seeded deterministic vectors (CANON.md:80): the seed's and the isolation suite's
stand-in for real embeddings. stdlib only; no model call, no numpy."""

from __future__ import annotations

import hashlib
import math
import struct

from app.core.config import EMBEDDING_DIMENSIONS

_UINT64_MAX = float(2**64 - 1)


def hash_vector(key: str, dims: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """sha256 of `key|counter` blocks → floats in [-1, 1] → unit-normalised."""
    if not key:
        raise ValueError("key must not be empty")
    if dims < 1:
        raise ValueError("dims must be at least 1")
    raw: list[float] = []
    counter = 0
    encoded = key.encode("utf-8")
    while len(raw) < dims:
        digest = hashlib.sha256(encoded + b"|" + str(counter).encode("ascii")).digest()
        for (word,) in struct.iter_unpack(">Q", digest):
            raw.append(word / _UINT64_MAX * 2.0 - 1.0)
        counter += 1
    raw = raw[:dims]
    norm = math.sqrt(sum(x * x for x in raw))
    if norm == 0.0:  # pragma: no cover - needs a sha256 preimage of all-midpoint words
        raise ValueError("degenerate vector")
    return [x / norm for x in raw]
