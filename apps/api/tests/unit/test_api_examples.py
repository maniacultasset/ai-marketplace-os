"""S3 T9 (AC12): every success and declared error example validates against its schema."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from app.api.errors import ErrorEnvelope
from app.api.v1.examples import CALLBACK_LOCATION, SUCCESS_EXAMPLES
from app.api.v1.surface import OPS, Operation, declared_responses, success_response

OP_LIST = list(OPS.values())


def test_every_op_has_example() -> None:
    assert set(SUCCESS_EXAMPLES) == set(OPS)


@pytest.mark.parametrize("op", OP_LIST, ids=[o.op_id for o in OP_LIST])
def test_examples_validate(op: Operation) -> None:
    example = SUCCESS_EXAMPLES[op.op_id]
    if op.response_model is None:
        assert op.status in {204, 303}
    else:
        op.response_model.model_validate(example)
        declared = success_response(op)[op.status]["content"]["application/json"]["example"]
        assert declared == example
    for status, response in declared_responses(op).items():
        body = response["content"]["application/json"]["example"]
        envelope = ErrorEnvelope.model_validate(body)
        if status != "default":
            assert envelope.error.status == status
            assert envelope.error.type == op.error_codes[int(status)][0]


def test_products_list_example_has_two_products() -> None:
    assert len(SUCCESS_EXAMPLES["products_list"]["data"]) == 2


def test_validate_example_is_a_rules_failure() -> None:
    assert SUCCESS_EXAMPLES["listings_validate"]["passed"] is False


def test_callback_location_carries_no_secret() -> None:
    op = OPS["marketplaces_callback"]
    header = success_response(op)[303]["headers"]["Location"]
    for location in (CALLBACK_LOCATION, header["example"], SUCCESS_EXAMPLES[op.op_id]["location"]):
        parts = urlsplit(location)
        assert not parts.scheme and not parts.netloc
        assert set(parse_qs(parts.query)) == {"pending"}
        lowered = location.lower()
        for word in ("code=", "state=", "token", "account", "seller"):
            assert word not in lowered
