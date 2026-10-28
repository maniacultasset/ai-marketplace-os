from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.listing import CanonicalListing
from pydantic import ValidationError

from mkos_pipeline.nodes.events import emit
from mkos_pipeline.state import GraphState


def _schema_report(exc: ValidationError) -> ErrorReport:
    items = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        items.append(
            ErrorItem(
                field=location or "*",
                error_code=ErrorCode.SCHEMA_INVALID,
                message=error.get("msg", "schema invalid")[:1000],
                retryable=True,
                source="schema",
                severity="error",
            )
        )
    return ErrorReport(status="failed", error_code=ErrorCode.SCHEMA_INVALID, errors=items)


async def validate_schema(state: GraphState) -> None:
    await emit(state, "validate_schema", "started")
    if state.draft is None:
        await emit(state, "validate_schema", "failed", {"reason": "no_listing"})
        return
    try:
        CanonicalListing.model_validate(state.draft.model_dump())
    except ValidationError as exc:
        # One schema correction that does not count toward attempts_used.
        try:
            repaired = CanonicalListing.model_validate(state.draft.model_dump(exclude_none=True))
            state.draft = repaired
        except ValidationError:
            state.last_report = _schema_report(exc)
            state.draft = None
            await emit(state, "validate_schema", "failed", {"reason": "schema_invalid"})
            return
    await emit(state, "validate_schema", "ok")


async def validate_rules(state: GraphState, *, announce: bool = True) -> None:
    if announce:
        await emit(state, "validate_rules", "started")
    if state.draft is None:
        if announce:
            await emit(state, "validate_rules", "failed", {"reason": "no_listing"})
        return
    if state.attempts_used >= 3:
        if announce:
            await emit(state, "validate_rules", "failed", {"reason": "cap"})
        return
    report = await state.deps.validate_rules(state.draft)
    state.attempts_used += 1
    state.last_report = report
    if announce:
        await emit(
            state,
            "validate_rules",
            "ok" if report.status == "passed" else "failed",
            {"attempt": state.attempts_used, "status": report.status},
        )


async def validate_vision(state: GraphState) -> None:
    await emit(state, "validate_vision", "started")
    if not state.vision_ran and state.draft is not None:
        reports = await state.deps.validate_images(state.draft)
        if reports:
            state.image_reports = list(reports)
        state.vision_ran = True
    await emit(state, "validate_vision", "ok", {"reports": len(state.image_reports)})
