"""Structured logging: one JSON line per event on stdout (ADR-0001: Cloud Logging reads stdout).

Spec [168] fields: `timestamp` (ISO 8601, UTC), `severity` (the field Cloud Logging reads for the
level), `message`, `module` (the logger name), `request_id` and `tenant_id` when bound, then the
event's own keys at the top level. With an active span and `GCP_PROJECT_ID` set, the
`logging.googleapis.com/trace|spanId|trace_sampled` fields join the line to its trace.

structlog and the stdlib (`app.*`, uvicorn, SQLAlchemy) share one chain: a root handler whose
`ProcessorFormatter` runs the same processors for both, and `redact()` runs on every line after
the traceback is rendered, so no secret or personal value reaches stdout.

- `uvicorn.access` is off: the request middleware writes the one access line per request.
- SQLAlchemy never prints on its own: `DATABASE_ECHO` maps to the `sqlalchemy.engine` level, a
  no-op handler on the Engine logger stops `echo=True` from attaching SQLAlchemy's plain-text stdout
  handler, and bound parameters on echo lines are replaced before formatting.
- `configure_logging()` is idempotent: it replaces only the root handler it added itself.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, Final, TextIO

import structlog
from pydantic import BaseModel
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import Settings
from app.core.redaction import redact
from app.core.request_context import current_context

_OWN_HANDLER_ATTR: Final = "_mkos_root_handler"
_SQLA_ENGINE: Final = "sqlalchemy.engine"
_SQLA_ENGINE_INSTANCE: Final = "sqlalchemy.engine.Engine"
_SQLA_PARAMS_MSG: Final = "[%s] %r"
SQL_PARAMETERS_HIDDEN: Final = "[SQL parameters hidden]"

TraceFieldsHook = Callable[[], Mapping[str, object] | None]
_trace_fields: TraceFieldsHook | None = None


class _RootHandler(logging.StreamHandler[TextIO]):
    """A stream handler that resolves `sys.stdout` at emit time unless a stream was given, so a
    replaced stdout (a test's capture, a reloaded process) is always the one written to."""

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__(stream if stream is not None else sys.stdout)
        self._fixed = stream is not None

    def emit(self, record: logging.LogRecord) -> None:
        if not self._fixed:
            self.stream = sys.stdout
        super().emit(record)


class _HideSqlParameters(logging.Filter):
    """The echo line that renders a statement's bound parameters (`[%s] %r`) keeps only its
    cache stats: a parameter can be a password hash or an email, which no key-based rule sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name.startswith(_SQLA_ENGINE)
            and record.msg == _SQLA_PARAMS_MSG
            and isinstance(record.args, tuple)
            and len(record.args) == 2
        ):
            record.msg = "[%s] " + SQL_PARAMETERS_HIDDEN
            record.args = (record.args[0],)
        return True


def set_trace_fields_hook(hook: TraceFieldsHook | None) -> None:
    """Tracing registers the function that returns the current span's Cloud Logging fields."""
    global _trace_fields
    _trace_fields = hook


_RECORD_KEYS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "color_message",
    "taskName",
}
"""LogRecord attributes that are not `extra`: the standard ones, the ones another handler's
formatter sets on the shared record (`message`, `asctime`) and uvicorn's `color_message`."""


_LOG_KWARGS: Final = frozenset({"exc_info", "stack_info", "stacklevel", "event"})


def _rename_reserved_keys(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """A structlog key that names a LogRecord attribute (`module`, `name`, ...) would make
    `logging` refuse the record; it is kept under `<key>_field` instead."""
    clashing = [k for k in event_dict if k in _RECORD_KEYS and k not in _LOG_KWARGS]
    for key in clashing:
        event_dict[f"{key}_field"] = event_dict.pop(key)
    return event_dict


def _add_extra(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    record = event_dict.get("_record")
    if record is not None:
        for key, value in record.__dict__.items():
            if key not in _RECORD_KEYS and not key.startswith("_"):
                event_dict.setdefault(key, value)
    return event_dict


def _add_request_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    ctx = current_context()
    if ctx is not None:
        event_dict.setdefault("request_id", ctx.request_id)
        if ctx.tenant_id is not None:
            event_dict.setdefault("tenant_id", ctx.tenant_id)
    return event_dict


def _add_trace_fields(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    hook = _trace_fields
    if hook is not None:
        fields = hook()
        if fields:
            for key, value in fields.items():
                event_dict.setdefault(key, value)
    return event_dict


_JSON_NATIVE: Final = (str, int, float, bool, type(None))


def _plain(value: Any, depth: int = 0) -> Any:
    """JSON-native values, so `redact()` sees the text of every object (its `repr`) before the
    renderer would turn it into text that no rule checks. A pydantic model or a dataclass becomes
    a dict first, so the key layer redacts its secret fields."""
    if isinstance(value, _JSON_NATIVE):
        return value
    if depth > 32:
        return "<...>"
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        return _plain(fields, depth + 1)
    if isinstance(value, Mapping):
        return {
            (k if isinstance(k, _JSON_NATIVE) else repr(k)): _plain(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item, depth + 1) for item in value]
    return repr(value)


def _redact(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    return dict(redact(_plain(event_dict)))


def _finalise(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """`level` -> `severity` (upper case), `event` -> `message`, `logger` -> `module`, in that
    order at the front of the line."""
    out: MutableMapping[str, Any] = {}
    out["timestamp"] = event_dict.pop("timestamp", None)
    out["severity"] = str(event_dict.pop("level", "info")).upper()
    out["message"] = event_dict.pop("event", "")
    out["module"] = event_dict.pop("logger", None)
    for key, value in event_dict.items():
        # An event key never overwrites one of the four fields above.
        out[f"{key}_field" if key in out else key] = value
    return dict(out)


def _shared_processors() -> list[Processor]:
    """Applied to structlog and stdlib events alike, after the record is unwrapped."""
    return [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_request_context,
        _add_trace_fields,
        structlog.processors.format_exc_info,
        _redact,
        _finalise,
    ]


def _renderer(settings: Settings) -> Processor:
    if settings.log_format == "console":
        return structlog.dev.ConsoleRenderer(colors=False, event_key="message")
    return structlog.processors.JSONRenderer(sort_keys=False, ensure_ascii=False)


def _drop_own_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _OWN_HANDLER_ATTR, False):
            root.removeHandler(handler)
            handler.close()


def _quiet_libraries(settings: Settings) -> None:
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        lib = logging.getLogger(name)
        lib.handlers.clear()
        lib.propagate = True
        lib.disabled = False
    engine = logging.getLogger(_SQLA_ENGINE)
    engine.handlers.clear()
    engine.propagate = True
    engine.setLevel(logging.INFO if settings.database_echo else logging.WARNING)
    # SQLAlchemy adds its own stdout handler to the Engine logger when `echo=True` and the logger
    # has none; a NullHandler there keeps every SQL line on the JSON chain (via root) only.
    instance = logging.getLogger(_SQLA_ENGINE_INSTANCE)
    instance.handlers.clear()
    instance.addHandler(logging.NullHandler())
    instance.propagate = True
    # S20: httpx logs every request line (URL with seller id and SKU) at INFO; the marketplace
    # client writes its own `marketplace.call` line instead, so the HTTP libraries say nothing
    # below WARNING.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Route structlog and the stdlib through one redacting JSON (or console) chain."""
    shared = _shared_processors()
    pre_chain: list[Processor] = [
        _add_extra,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[*shared, _renderer(settings)],
    )
    handler = _RootHandler(stream)
    setattr(handler, _OWN_HANDLER_ATTR, True)
    handler.setFormatter(formatter)
    handler.addFilter(_HideSqlParameters())

    root = logging.getLogger()
    _drop_own_handlers(root)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    _quiet_libraries(settings)

    # structlog events become ordinary stdlib records (message text, `extra`, a real `exc_info`),
    # so every handler on root sees one shape: this chain, and Sentry's logging integration
    # (whose event then carries the exception and the extras, both redacted in `before_send`).
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            _rename_reserved_keys,
            structlog.stdlib.render_to_log_kwargs,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A structlog logger named after its module (the line's `module` field)."""
    return structlog.stdlib.get_logger(name)
