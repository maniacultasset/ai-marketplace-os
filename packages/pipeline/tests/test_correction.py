from tests.fakes import FakeDeps, ProductAwareTransport, make_context

from mkos_pipeline.graph import run_listing_pipeline
from mkos_pipeline.llm.client import reset_transport, set_transport


async def test_correction_prompt_contains_only_failing_field() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)
    seen: list[dict] = []

    class Spy(ProductAwareTransport):
        def __init__(self) -> None:
            super().__init__(long_title=True)

        async def __call__(self, payload, timeout):
            user = payload["messages"][1]["content"]
            if "rejected listing field" in user.lower():
                seen.append(payload)
            return await super().__call__(payload, timeout)

    token = set_transport(Spy())
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    assert result.terminal_state == "blocked"
    assert seen
    user = seen[0]["messages"][1]["content"]
    assert "content.title" in user
    assert "FIELD_TOO_LONG" in user
    assert "category_template" not in user
    assert "previous_version" not in user
