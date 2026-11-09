"""Job payloads v1 of the publish family (S21 CP11; DW8; EXECUTION-PLAN.md:291-293).

`job_runs.payload_json` of a `listing_publish` job is `PublishJobPayload` and of a
`listing_status_sync` job `StatusSyncPayload`. Both ignore unknown keys, so the previous worker
still runs a job the new API enqueued; a change is additive only (a new optional key), and a `v`
other than 1 is refused (`ValueError`), never guessed. The retry paths are computed by the job from
`publish_field_results` (DATABASE.md:887), never carried in the payload.

SI11 (ADR-0028): `trigger = "resolution"` marks a job the resolution engine enqueued (an automatic
resend, a re-check); the engine counts those jobs for its 3-cycle cap. A `listing_status_sync` job
with `op = "preview"` is the marketplace dry run of `version_id`, not a status read.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from mkos_schemas.jobs import MAX_TARGET_FIELDS
from pydantic import BaseModel, ConfigDict, Field

PublishMode = Literal["publish", "retry_fields", "resume"]
SyncReason = Literal["scheduled", "refresh", "daily"]
Trigger = Literal["user", "resolution"]
SyncTrigger = Literal["system", "resolution"]
SyncOp = Literal["sync", "preview"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    v: Literal[1] = 1


class PublishJobPayload(_Payload):
    listing_id: UUID
    listing_version_id: UUID
    mode: PublishMode = "publish"
    trigger: Trigger = "user"


class StatusSyncPayload(_Payload):
    listing_id: UUID
    reason: SyncReason = "scheduled"
    op: SyncOp = "sync"
    version_id: UUID | None = None
    trigger: SyncTrigger = "system"
    # fix round 3: a preview's inputs key (version, repairs, seller facts); a replay is taken
    # only from the version's newest preview, and only when this matches
    basis: str | None = None


class AutoFixPayload(_Payload):
    """SI11 T15: a `listing_generate` job the resolution engine enqueued to fix content. Stored
    whole (never through `dump`): `trigger` must stay in `payload_json`, so the 3-cycle cap counts
    the job and the human mark never takes it for a member's request."""

    listing_id: UUID
    mode: Literal["auto_fix", "ai_field"] = "auto_fix"
    base_version_id: UUID
    issue_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    trigger: Literal["resolution"] = "resolution"
    # T16 (`ai_field`): the paths the model may rewrite and one feedback line per refusal
    target_fields: tuple[str, ...] = Field(default=(), max_length=MAX_TARGET_FIELDS)
    feedback: tuple[str, ...] = Field(default=(), max_length=5)


# SI11 keys left out at their default, so every payload written before SI11 (and every user job
# since) stays byte-equal: an older worker, the fixtures and the job keys see no change.
_OMIT_AT_DEFAULT = frozenset({"trigger", "op", "version_id", "basis"})


def dump(payload: _Payload) -> dict[str, Any]:
    """The JSON stored in `job_runs.payload_json`."""
    data = payload.model_dump(mode="json")
    fields = type(payload).model_fields
    for key in _OMIT_AT_DEFAULT & set(data):
        if getattr(payload, key) == fields[key].default:
            del data[key]
    return data


class DiagnosisPayload(_Payload):
    """SI13 T12: a `brand_embed` job with `entity_type 'error_proposal'` that drafts one
    error-map proposal (CP-D1). Holds ids only, never marketplace text."""

    op: Literal["diagnose"] = "diagnose"
    proposal_id: UUID
    sample_issue_id: UUID
