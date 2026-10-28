from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

JOB_INTERNAL_BUDGET = timedelta(seconds=270)
STOP_CORRECTIONS_WHEN = timedelta(seconds=90)
SKIP_MODEL_WHEN = timedelta(seconds=5)
MAX_MODEL_TIMEOUT = 40


@dataclass(frozen=True, slots=True)
class Deadline:
    started_at: datetime
    deadline_at: datetime

    @classmethod
    def start(cls, now: datetime | None = None) -> "Deadline":
        started = now or datetime.now(UTC)
        return cls(started_at=started, deadline_at=started + JOB_INTERNAL_BUDGET)


def seconds_left(deadline_at: datetime, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    return (deadline_at - current).total_seconds()
