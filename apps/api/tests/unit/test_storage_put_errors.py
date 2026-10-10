"""S26 (KNOWN-ISSUES §E, S13b): `ObjectStorage.put` maps a 5xx or an unreachable store to the
retryable `StorageUnavailable`, as `get` does; any other client error is raised as it is."""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.storage import ObjectStorage, StorageUnavailable
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)


class _Client:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def put_object(self, **_: Any) -> None:
        raise self.error


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


@pytest.mark.parametrize(
    "error",
    [
        _client_error("InternalError", 500),
        _client_error("SlowDown", 503),
        EndpointConnectionError(endpoint_url="http://localhost:1"),
    ],
    ids=["500", "503", "unreachable"],
)
async def test_storage_put_unavailable(error: Exception) -> None:
    with pytest.raises(StorageUnavailable):
        await ObjectStorage(_Client(error)).put("b", "k", b"x", "text/csv")


async def test_storage_put_other_client_error_raised() -> None:
    with pytest.raises(ClientError):
        await ObjectStorage(_Client(_client_error("AccessDenied", 403))).put(
            "b", "k", b"x", "text/csv"
        )
