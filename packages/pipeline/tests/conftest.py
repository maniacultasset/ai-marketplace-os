import pytest

from mkos_pipeline.llm.client import reset_transport, set_transport


@pytest.fixture(autouse=True)
def _clear_llm_transport():
    previous = set_transport(None)
    yield
    reset_transport(previous)
