"""Shared API string types."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AfterValidator, Field
from pydantic_core import PydanticCustomError

STORAGE_KEY_MAX: Final = 512


def _storage_key_shape(value: str) -> str:
    # The DB CHECK on images/brand_documents storage keys refuses any `..` and `//` (0002); the API
    # refuses the same, plus a trailing `/`, before a key can reach a read. The tenant prefix is
    # checked by the owner step against the prefix derived from `tid` (S12/S13/S17b).
    if ".." in value or "//" in value or value.endswith("/"):
        raise PydanticCustomError(
            "mkos_invalid", "Storage key must not contain '..' or '//' or end with '/'."
        )
    return value


StorageKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=STORAGE_KEY_MAX,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
        description=(
            "An object key under the caller's tenant prefix, never a URL: ASCII letters, digits "
            "and `._/-`, no `..`, no `//`, no leading or trailing `/`."
        ),
    ),
    AfterValidator(_storage_key_shape),
]
