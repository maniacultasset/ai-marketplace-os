"""S13 A12 (Q3): the Idempotency-Key request hash."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import HTTP

from app.services.idempotency import request_hash


def test_request_hash_vector() -> None:
    # sha256(b"POST /api/v1/jobs/x/cancel" + b"\x00" + b"{}"); the method is upper-cased
    assert (
        request_hash("post", "/api/v1/jobs/x/cancel", b"{}").hex()
        == "cf6920f3425a1fa22a472b9a5ad34fae58b8229617607f577eff86d5126693f7"
    )


def test_request_hash_separates_endpoint_and_body() -> None:
    assert request_hash("POST", "/a", b"b") != request_hash("POST", "/ab", b"")
    assert request_hash("POST", "/a", b"x") != request_hash("POST", "/a", b"y")
    assert request_hash("POST", "/a", b"x") != request_hash("PUT", "/a", b"x")


def _multipart(boundary: str, data: bytes) -> bytes:
    return (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="c.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _file_part(body: bytes, boundary: str) -> bytes:
    head = f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + body)
    [part] = list(message.iter_parts())
    payload = part.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload


def test_multipart_hash_ignores_boundary() -> None:
    data = b"seller_sku,product_name,category\r\nA-1,Kurta,ethnic_wear_kurta_sets\r\n"
    one, two = _multipart("aaaa1111", data), _multipart("bbbb2222", data)
    assert one != two
    path = "/api/v1/products/bulk"
    raw = request_hash("POST", path, one), request_hash("POST", path, two)
    assert raw[0] != raw[1]  # hashing the raw body would make every replay a mismatch
    parts = _file_part(one, "aaaa1111"), _file_part(two, "bbbb2222")
    assert parts[0] == parts[1] == data
    assert request_hash("POST", path, parts[0]) == request_hash("POST", path, parts[1])


def test_key_reused_is_the_idempotency_conflict() -> None:
    """S26 T23 gate-fix (AC 11): the code and the constraint the 409 maps from."""
    from app.core.errors import ConflictError
    from app.services.idempotency import key_reused
    from mkos_schemas.codes import ErrorCode

    error = key_reused()
    assert isinstance(error, ConflictError)
    assert (error.code, error.constraint) == (ErrorCode.IDEMPOTENCY_KEY_REUSED, "idempotency_keys")
