"""The variant photo match (V1.1, client feedback item 9: a colour's photos must show this product
in this colour, not merely pass the technical checks).

`queue_photo_match(uow, image_id)` runs in its own short unit of work right after the upload checks
stored a photo's clean copy (`app.services.image_compliance`). It queues at most one
`image_compliance` job about a `photo_match` (entity = the image), and only when:

- `PHOTO_MATCH_ENABLED` is on;
- the photo is live, a product photo (`main`, `secondary` or `lifestyle`), has its clean copy and
  passed its technical checks (`compliance_status = 'passed'`);
- its match key (the image, its clean copy's hash and the colour it is checked against) is not
  the one already stored on it: one image content and colour is checked once (invariant 10). A
  check that ended with no call (`retry`: the switch went off, no entry point, a budget refusal)
  may be queued again. A key whose job already ended (the colour changed back) takes a fresh job
  key derived from that job.

Besides the upload, `queue_product_photo_matches` queues a product's due checks when the seller
edits the colour fact and when a listing is generated or regenerated: a check left with no call,
a changed colour, and a photo uploaded before the switch went on are checked then.

The colour is the photo's colourway colour; on a product without colourways, the product's own
colour fact; a photo shared by every colour of a colour style is checked for the product only.
Queueing stores `metadata.photo_match = {status: "checking", ...}` on the image, so the product
read shows the check as running. The worker (`app.workers.photo_match`) stores the verdict there.

The verdict (`match`, `mismatch` or `unsure`) comes from the pipeline's fixed score bands. On a
`mismatch` or `unsure` the photo carries a question for the seller ("Is this photo the Green
Cotton Kurta?"); the seller keeps the photo (`POST /images/{image_id}/photo-match/keep`, recorded
with who and when, and audited) or removes it. At approval an unresolved `mismatch` blocks and an
unresolved `unsure`, a check still running and a kept `mismatch` warn (`app.services.image_gate`).
A `checking` match reads by its job (`live_jobs`): running while the job is queued or running.

Pure helpers here read and shape the stored value; nothing here calls a model.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.jobs import TERMINAL, EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.workers.policy import PHOTO_MATCH_ENTITY, lock_for

__all__ = [
    "ASKS",
    "CHECKING_STALE",
    "MATCH_SLOTS",
    "GateVerdict",
    "MatchStatus",
    "PhotoMatchState",
    "colour_of",
    "job_of",
    "keep_photo",
    "live_jobs",
    "match_key",
    "question_for",
    "queue_photo_match",
    "queue_product_photo_matches",
    "read_state",
    "state_fields",
    "unresolved",
]

log = get_logger(__name__)
KEY_VERSION: Final = "photo_match:v1"
MATCH_SLOTS: Final = frozenset({"main", "secondary", "lifestyle"})
MatchStatus = Literal["checking", "match", "mismatch", "unsure", "failed"]
STATUSES: Final[frozenset[str]] = frozenset({"checking", "match", "mismatch", "unsure", "failed"})
# The verdicts that ask the seller.
ASKS: Final = frozenset({"mismatch", "unsure"})
# A `checking` match reads by its job: running while the job is `queued` or `running`, not checked
# once the job ended without storing a verdict (dead-lettered, cancelled). A reader that did not
# look the job up falls back to this clock: a check still `checking` after this long reads as not
# checked, never as running forever. Long past the per-brand AI lock's queue and the job's tries.
CHECKING_STALE: Final = timedelta(hours=6)
# How many already-finished jobs one queue walks past for a fresh job key (a colour changed back)
FRESH_KEY_TRIES: Final = 8
TITLE_WORDS: Final = 60


def fold_colour(colour: str | None) -> str:
    return " ".join((colour or "").split()).casefold()


def match_key(image_id: uuid.UUID, clean_hash: str, colour: str | None) -> str:
    """64 hex: one check per image content and colour."""
    body = "\n".join([KEY_VERSION, str(image_id), clean_hash, fold_colour(colour)])
    return hashlib.sha256(body.encode()).hexdigest()


def colour_of(attributes: Any) -> str | None:
    """The `color` fact's text value, else None."""
    fact = attributes.get("color") if isinstance(attributes, Mapping) else None
    value = fact.get("value") if isinstance(fact, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:60]


def question_for(colour: str | None, title: str) -> str:
    """The seller's question, in plain words: "Is this photo the Green Cotton Kurta?"."""
    name = " ".join(title.split())
    if len(name) > TITLE_WORDS:
        name = name[: TITLE_WORDS - 1].rsplit(" ", 1)[0] + "…"
    subject = f"{colour} {name}" if colour else name
    return f"Is this photo the {subject}?"


@dataclass(frozen=True, slots=True)
class PhotoMatchState:
    """The stored match of one image as the product read and the approval gate see it."""

    status: MatchStatus
    colour: str | None = None
    product_match: float | None = None
    colour_match: float | None = None
    seen_colour: str | None = None
    reason: str | None = None
    question: str | None = None
    checked_at: datetime | None = None
    kept_at: datetime | None = None
    kept_by: uuid.UUID | None = None

    @property
    def kept(self) -> bool:
        return self.kept_at is not None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _user(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def job_of(raw: Any) -> uuid.UUID | None:
    """The job id a stored `checking` match waits on, else None."""
    if not isinstance(raw, Mapping) or raw.get("status") != "checking":
        return None
    return _user(raw.get("job_id"))


async def live_jobs(uow: TenantUnitOfWork, raws: Any) -> set[uuid.UUID]:
    """The jobs still `queued` or `running` among those the stored `checking` matches wait on."""
    ids = [job for raw in raws if (job := job_of(raw)) is not None]
    return await uow.job_runs.live_among(ids) if ids else set()


def read_state(
    raw: Any, *, clean_hash: str | None, now: datetime, live: set[uuid.UUID] | None = None
) -> PhotoMatchState | None:
    """The stored `metadata.photo_match` of an image, or None when there is none, it is malformed,
    or it was made for other pixels (`source_hash` is not the image's clean hash). A `checking`
    reads by its job when `live` (the live job ids, `live_jobs`) is given: still `checking` while
    the job is live, `failed` (not checked) once it ended; without `live`, a `checking` left for
    `CHECKING_STALE` reads `failed`."""
    if not isinstance(raw, Mapping):
        return None
    found = raw.get("status")
    if found not in STATUSES or clean_hash is None or raw.get("source_hash") != clean_hash:
        return None
    status: MatchStatus = found
    if status == "checking":
        if live is not None:
            job = job_of(raw)
            if job is None or job not in live:
                status = "failed"
        else:
            queued = _when(raw.get("queued_at"))
            if queued is None or now - queued > CHECKING_STALE:
                status = "failed"
    kept_at = _when(raw.get("kept_at")) if status in ASKS else None
    return PhotoMatchState(
        status=status,
        colour=_text(raw.get("colour"), 60),
        product_match=_score(raw.get("product_match")),
        colour_match=_score(raw.get("colour_match")),
        seen_colour=_text(raw.get("seen_colour"), 40),
        reason=_text(raw.get("reason"), 160),
        question=_text(raw.get("question"), 200) if status in ASKS else None,
        checked_at=_when(raw.get("checked_at")),
        kept_at=kept_at,
        kept_by=_user(raw.get("kept_by")) if kept_at is not None else None,
    )


GateVerdict = Literal["mismatch", "unsure", "checking", "kept_mismatch"]


def unresolved(state: PhotoMatchState | None) -> GateVerdict | None:
    """What the approval gate judges: an unkept `mismatch` (blocks); an unkept `unsure`, a check
    still running, or a `mismatch` the seller kept (each warns: the approver sees the photo was
    questioned and kept, maker-checker). None when there is nothing to say."""
    if state is None:
        return None
    if state.status == "checking":
        return "checking"
    if state.status not in ASKS:
        return None
    if state.kept:
        return "kept_mismatch" if state.status == "mismatch" else None
    return "mismatch" if state.status == "mismatch" else "unsure"


async def _colour(
    uow: TenantUnitOfWork, product: Any, colourway_id: uuid.UUID | None
) -> str | None:
    if colourway_id is not None:
        for row in await uow.colourways.colourways_of(product.id):
            if row.id == colourway_id:
                return " ".join(row.colour.split())[:60] or None
        return None
    if await uow.colourways.colourways_of(product.id):
        return None  # a photo every colour shares: the product only
    return colour_of(product.attributes)


async def queue_photo_match(uow: TenantUnitOfWork, image_id: uuid.UUID) -> uuid.UUID | None:
    """The queued (or already queued) check's job id, or None when no check is due."""
    if not get_settings().photo_match_enabled:
        return None
    image = await uow.images.get_live(image_id)
    if image is None or image.clean_content_hash is None or image.slot not in MATCH_SLOTS:
        return None
    if image.compliance_status != "passed":
        return None
    product = await uow.products.get_by_id(image.product_id)
    if product is None:
        return None
    colour = await _colour(uow, product, image.colourway_id)
    key = match_key(image.id, image.clean_content_hash, colour)
    current = (image.metadata_ or {}).get("photo_match")
    job_key = key
    if isinstance(current, Mapping) and current.get("key") == key:
        if current.get("retry") is not True:
            return None  # this content and colour is checked, or its check is queued
        # a check that ended with no call (switch off, no entry point, budget) may run again,
        # under a job key of its own
        job_key = hashlib.sha256(f"{key}\n{current.get('job_id')}".encode()).hexdigest()
    tenant = uow.products.tenant_id
    for _ in range(FRESH_KEY_TRIES):
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=PHOTO_MATCH_ENTITY,
                entity_id=image.id,
                created_by=None,
                payload={"photo_match": True, "image_id": str(image.id), "match_key": key},
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, PHOTO_MATCH_ENTITY, tenant),
                job_key=job_key,
            )
        )
        if enqueued.created or enqueued.job.status not in TERMINAL:
            break
        # the key names a job that already ended (the colour changed back): that job never runs
        # again, so the check takes a key of its own, derived from the ended job
        job_key = hashlib.sha256(f"{key}\n{enqueued.job.id}".encode()).hexdigest()
    else:
        log.warning("photo_match.no_fresh_key", image_id=str(image.id))
        return None
    await uow.images.write_photo_match(
        image.id,
        {
            "status": "checking",
            "key": key,
            "source_hash": image.clean_content_hash,
            "colour": colour,
            "job_id": str(enqueued.job.id),
            "queued_at": datetime.now(UTC).isoformat(),
        },
        source_hash=image.clean_content_hash,
    )
    log.info(
        "photo_match.queued",
        image_id=str(image.id),
        job_id=str(enqueued.job.id),
        created=enqueued.created,
    )
    return enqueued.job.id


async def queue_product_photo_matches(uow: TenantUnitOfWork, product_id: uuid.UUID) -> int:
    """Queue the due checks of a product's live photos (`queue_photo_match` decides each): a
    colour fact that changed, a check that ended with no call (`retry`), or a photo uploaded
    before the switch went on. Called when the seller edits the colour and when a listing is
    generated or regenerated. Each photo in its own savepoint: a failed queue never fails the
    caller. The number of checks queued or already queued."""
    if not get_settings().photo_match_enabled:
        return 0
    queued = 0
    for row in await uow.images.listing_set(product_id):
        if row.slot not in MATCH_SLOTS:
            continue
        try:
            async with uow.session.begin_nested():
                if await queue_photo_match(uow, row.image_id) is not None:
                    queued += 1
        except Exception as error:
            log.warning(
                "photo_match.queue_failed", image_id=str(row.image_id), error=type(error).__name__
            )
    return queued


def state_fields(state: PhotoMatchState) -> dict[str, Any]:
    """The `PhotoMatchOut` fields of a stored match."""
    return {
        "status": state.status,
        "colour": state.colour,
        "product_match": state.product_match,
        "colour_match": state.colour_match,
        "seen_colour": state.seen_colour,
        "reason": state.reason,
        "question": state.question,
        "checked_at": state.checked_at,
        "kept": state.kept,
        "kept_at": state.kept_at,
        "kept_by_user_id": state.kept_by,
    }


def _json(fields: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if isinstance(value, uuid.UUID):
            out[name] = str(value)
        elif isinstance(value, datetime):
            out[name] = value.isoformat()
        else:
            out[name] = value
    return out


async def _keep(uow: TenantUnitOfWork, principal: Principal, image_id: uuid.UUID) -> dict[str, Any]:
    image = await uow.images.get_live(image_id)
    if image is None:
        raise NotFoundError("image_id")
    await uow.images.lock_product_images(image.product_id)
    now = datetime.now(UTC)
    raw = (image.metadata_ or {}).get("photo_match")
    state = read_state(raw, clean_hash=image.clean_content_hash, now=now)
    if state is None or state.status not in ASKS:
        # nothing to keep: no question on this photo (not checked, checking, or a match)
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "images")
    if not state.kept:
        if not await uow.images.keep_photo_match(image_id, user_id=principal.user_id, at=now):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "images")
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="image.photo_match_kept",
            entity_type="image",
            entity_id=image_id,
            reason=f"photo match {state.status}",
        )
        log.info("photo_match.kept", image_id=str(image_id), verdict=state.status)
        state = replace(state, kept_at=now, kept_by=principal.user_id)
    return {"image_id": str(image_id), "photo_match": _json(state_fields(state))}


async def keep_photo(
    principal: Principal, key: str, path: str, image_id: uuid.UUID
) -> dict[str, Any]:
    """`POST /images/{image_id}/photo-match/keep`: the seller keeps a photo the match questioned.
    Who and when join the stored match, and one `image.photo_match_kept` audit row is written; a
    second keep is 200 with the first keep's record. No question on the photo is 409
    `INVALID_STATUS_TRANSITION`. The route's idempotent transaction, stored under the key."""
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        return 200, await _keep(repositories_on(session, principal.tenant_id), principal, image_id)

    _status, stored = await idempotent(principal, key, "POST", path, b"{}", handler)
    return stored
