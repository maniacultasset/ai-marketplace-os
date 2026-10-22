"""The migrator's platform loaders (`python -m app.cli dictionary sync`, `templates load`; S13 B5,
B7; `errors load`, S20 T4; `rules load`; DATABASE.md §5.6 :1152-1179, §5.2 :543, §8.2). The SQL
lives here, inside `app/db` (repo rule R4); the CLI only orchestrates. Every loader runs as `migrator` in one transaction: the platform
tables are SELECT-only for the app roles.

**`sync_dictionary(engine)`** upserts `mkos_schemas/data/attribute_dictionary.json` into
`attribute_definitions`, `attribute_columns` and `category_attributes`. Each upsert updates a row
only when its content (every column but `revision` and `created_at`) differs (`... DO UPDATE ...
WHERE (t.cols) IS DISTINCT FROM (EXCLUDED.cols)`), so a re-sync of an unchanged file writes no row
and never moves `revision`, and with it `rules_version` (DATABASE.md:507, :823). A key that left
the file is disabled, never deleted. The loader checks the file first: a `column` key maps at
least one typed column, an `attributes` key none, and no column maps twice.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, FixAction, Marketplace, RetryClass, Severity
from mkos_schemas.jobs import CategoryTemplate as CategoryTemplateModel
from mkos_schemas.paths import is_valid_field_path
from mkos_schemas.rules import RuleSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)
from sqlalchemy import Connection, func, literal_column, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models.platform import (
    AttributeColumn,
    AttributeDefinition,
    CategoryAttribute,
    CategoryTemplate,
    MarketplaceErrorMap,
)
from app.db.models.rules import Rule

DICTIONARY_RESOURCE: Final = ("mkos_schemas", "data/attribute_dictionary.json")
Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
Level = Literal["brand", "style", "colour", "variant", "standalone"]
Source = Literal["seller", "document", "pim", "marketplace", "derived", "ai"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnRef(_Strict):
    table: Literal["products", "brand_profiles"]
    column: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]


class DictionaryEntry(_Strict):
    key: Key
    section: Literal[
        "identity", "classification", "attributes", "care", "compliance", "commercial", "dimensions"
    ]
    storage: Literal["column", "attributes"]
    owner_levels: Annotated[list[Level], Field(min_length=1)]
    value_type: Literal[
        "text", "text_list", "enum", "enum_list", "number", "integer", "boolean", "money",
        "measure", "composition", "object", "party",
    ]  # fmt: skip
    unit: str | None = None
    vocabulary: list[str] | None = None
    input_aliases: list[str] = Field(default_factory=list)
    is_critical: bool
    allowed_sources: Annotated[list[Source], Field(min_length=1)]
    min_confidence: Annotated[Decimal | None, Field(ge=0, le=1, decimal_places=2)] = None
    confirm_agent_captures: bool
    brd_ref: str | None = None
    enabled: bool = True
    columns: list[ColumnRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _storage_columns(self) -> DictionaryEntry:
        if (self.storage == "column") != bool(self.columns):
            raise ValueError(f"{self.key}: a column key maps >= 1 column, an attributes key none")
        if self.is_critical and ("ai" in self.allowed_sources or not self.confirm_agent_captures):
            raise ValueError(f"{self.key}: a critical key never allows ai and confirms captures")
        return self


class CategoryAttributeEntry(_Strict):
    category: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{2,64}$")]
    attribute_key: Key
    relevance: Literal["core", "common", "optional"]
    critical_override: Literal[True] | None = None
    display_order: int = 0
    enabled: bool = True


class DictionaryFile(_Strict):
    dictionary_version: Annotated[int, Field(ge=1)]
    attributes: Annotated[list[DictionaryEntry], Field(min_length=1)]
    category_attributes: list[CategoryAttributeEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> DictionaryFile:
        keys = [entry.key for entry in self.attributes]
        if len(keys) != len(set(keys)):
            raise ValueError("a dictionary key appears twice")
        columns = [(c.table, c.column) for entry in self.attributes for c in entry.columns]
        if len(columns) != len(set(columns)):
            raise ValueError("a typed column maps to two keys")
        aliases = [a for entry in self.attributes for a in entry.input_aliases]
        if len(aliases) != len(set(aliases)) or set(aliases) & set(keys):
            raise ValueError("an input alias is used twice or shadows a key")
        known = set(keys)
        for row in self.category_attributes:
            if row.attribute_key not in known:
                raise ValueError("a category attribute names an unknown key")
        return self


def load_dictionary(path: Path | None = None) -> DictionaryFile:
    if path is None:
        package, name = DICTIONARY_RESOURCE
        text = resources.files(package).joinpath(name).read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return DictionaryFile.model_validate(json.loads(text))


@dataclass(frozen=True, slots=True)
class SyncCounts:
    inserted: int
    updated: int
    disabled: int


_DEFINITION_COLUMNS: Final = (
    "section",
    "storage",
    "owner_levels",
    "value_type",
    "unit",
    "vocabulary",
    "input_aliases",
    "is_critical",
    "allowed_sources",
    "min_confidence",
    "confirm_agent_captures",
    "brd_ref",
    "enabled",
)
_CATEGORY_COLUMNS: Final = ("relevance", "critical_override", "display_order", "enabled")


def _definition_row(entry: DictionaryEntry) -> dict[str, Any]:
    row = entry.model_dump(exclude={"columns"})
    row["owner_levels"] = list(entry.owner_levels)
    row["allowed_sources"] = list(entry.allowed_sources)
    return row


async def _upsert(
    connection: AsyncConnection,
    model: Any,
    rows: Sequence[dict[str, Any]],
    index: Sequence[str],
    compared: Sequence[str],
) -> tuple[int, int]:
    """(inserted, updated): a conflicting row is updated only when a compared column differs."""
    if not rows:
        return 0, 0
    stmt = insert(model).values(list(rows))
    table = model.__table__
    changed = tuple_(*(table.c[name] for name in compared)).is_distinct_from(
        tuple_(*(stmt.excluded[name] for name in compared))
    )
    upsert: Any = stmt.on_conflict_do_update(
        index_elements=list(index),
        set_={name: stmt.excluded[name] for name in compared},
        where=changed,
    ).returning(
        literal_column("(xmax = 0)")
    )  # xmax 0: the row was inserted, not updated
    flags = [bool(flag) for flag in (await connection.execute(upsert)).scalars()]
    inserted = sum(flags)
    return inserted, len(flags) - inserted


def _assume_migrator(connection: Connection) -> None:
    from app.db.migrations import assume_migration_role

    assume_migration_role(connection)


async def sync_rows(connection: AsyncConnection, dictionary: DictionaryFile) -> SyncCounts:
    """The sync on an open migrator transaction (`sync_dictionary` opens one)."""
    await connection.run_sync(_assume_migrator)
    definitions = [_definition_row(e) for e in dictionary.attributes]
    inserted, updated = await _upsert(
        connection, AttributeDefinition, definitions, ["key"], _DEFINITION_COLUMNS
    )
    keys = [e.key for e in dictionary.attributes]
    disabled = await connection.execute(
        update(AttributeDefinition)
        .where(AttributeDefinition.key.not_in(keys), AttributeDefinition.enabled.is_(True))
        .values(enabled=False)
    )
    columns = [
        {"table_name": c.table, "column_name": c.column, "attribute_key": e.key}
        for e in dictionary.attributes
        for c in e.columns
    ]
    col_inserted, col_updated = await _upsert(
        connection, AttributeColumn, columns, ["table_name", "column_name"], ["attribute_key"]
    )
    categories = [row.model_dump() for row in dictionary.category_attributes]
    cat_inserted, cat_updated = await _upsert(
        connection,
        CategoryAttribute,
        categories,
        ["category", "attribute_key"],
        _CATEGORY_COLUMNS,
    )
    return SyncCounts(
        inserted=inserted + col_inserted + cat_inserted,
        updated=updated + col_updated + cat_updated,
        disabled=int(getattr(disabled, "rowcount", 0) or 0),
    )


async def sync_dictionary(engine: AsyncEngine, path: Path | None = None) -> SyncCounts:
    """`dictionary sync`: the whole file in one migrator transaction."""
    dictionary = load_dictionary(path)
    async with engine.begin() as connection:
        return await sync_rows(connection, dictionary)


# ---- `templates load` (S13 B7; PD11; DATABASE.md §5.2 category_templates :523-545) ----
#
# `category_templates` is versioned (`ct_version_key (marketplace, category, version)`, one
# `is_current` per pair) and the 0004 guard keeps content immutable, so there is no upsert. Each
# entry, keyed on `(marketplace, category, version)`: the same key with the same content
# (canonical `attributes`, `external_category_id`, `product_type`) is a no-op; the same key with
# other content refuses the whole file ("bump version"), before any row is written; a new
# version is inserted not current (`source='manual'`, no `raw_schema_key`) and promoted when it is
# newer than the current one. The whole file is one transaction.

TEMPLATE_SOURCE: Final = "manual"


class TemplateLoad(_Strict):
    version: Annotated[int, Field(ge=1)]
    product_type: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    template: CategoryTemplateModel


class TemplateConflict(Exception):  # noqa: N818 - the CLI names it in its exit line
    """A stored version holds other content: the file must bump the version."""

    def __init__(self, marketplace: str, category: str, version: int) -> None:
        super().__init__(f"{marketplace}/{category} v{version}")
        self.key = f"{marketplace}/{category} v{version}"


@dataclass(frozen=True, slots=True)
class TemplateCounts:
    inserted: int
    unchanged: int
    promoted: int


def load_template_file(path: Path) -> list[TemplateLoad]:
    """The file as `list[TemplateLoad]`; `FileNotFoundError` and `ValidationError` propagate."""
    return TypeAdapter(list[TemplateLoad]).validate_json(path.read_bytes())


def _template_content(entry: TemplateLoad) -> dict[str, Any]:
    body: dict[str, Any] = entry.template.model_dump(
        mode="json", exclude={"marketplace", "category", "external_category_id"}
    )
    return body


async def promote_template(
    connection: AsyncConnection, marketplace: str, category: str, version: int
) -> bool:
    """Make `version` the current template of `(marketplace, category)`: the two ordered
    UPDATEs of DATABASE.md:543 (the old current off, then the new one on), in the caller's
    transaction, audited with a NULL tenant. False (and nothing written) when it is already
    current. `LookupError` when that version does not exist."""
    table = CategoryTemplate.__table__
    target = (
        await connection.execute(
            select(table.c.id, table.c.is_current).where(
                table.c.marketplace == marketplace,
                table.c.category == category,
                table.c.version == version,
            )
        )
    ).one_or_none()
    if target is None:
        raise LookupError("no such template version")
    if target.is_current:
        return False
    await connection.execute(
        update(CategoryTemplate)
        .where(
            CategoryTemplate.marketplace == marketplace,
            CategoryTemplate.category == category,
            CategoryTemplate.is_current.is_(True),
        )
        .values(is_current=False)
    )
    await connection.execute(
        update(CategoryTemplate).where(CategoryTemplate.id == target.id).values(is_current=True)
    )
    await connection.execute(
        text(
            "SELECT audit(NULL, 'system', NULL, 'category_template.promoted', "
            "'category_template', :id, NULL, CAST(:after AS jsonb), :reason, NULL, NULL, NULL, "
            "NULL)"
        ),
        {
            "id": target.id,
            "after": json.dumps({"marketplace": marketplace, "category": category}),
            "reason": f"templates load: version {version}",
        },
    )
    return True


async def load_template_rows(
    connection: AsyncConnection, entries: Sequence[TemplateLoad]
) -> TemplateCounts:
    """The load on an open transaction (`load_templates` opens one, as migrator)."""
    await connection.run_sync(_assume_migrator)
    table = CategoryTemplate.__table__
    inserted = unchanged = promoted = 0
    ordered = sorted(
        entries, key=lambda e: (e.template.marketplace.value, e.template.category, e.version)
    )
    for entry in ordered:
        marketplace, category = entry.template.marketplace.value, entry.template.category
        content = _template_content(entry)
        stored = (
            await connection.execute(
                select(table.c.attributes, table.c.external_category_id, table.c.product_type)
                .where(
                    table.c.marketplace == marketplace,
                    table.c.category == category,
                    table.c.version == entry.version,
                )
                .with_for_update()
            )
        ).one_or_none()
        if stored is not None:
            same = (
                stored.attributes == content
                and stored.external_category_id == entry.template.external_category_id
                and stored.product_type == entry.product_type
            )
            if not same:
                raise TemplateConflict(marketplace, category, entry.version)
            unchanged += 1
            continue
        await connection.execute(
            insert(CategoryTemplate).values(
                marketplace=marketplace,
                category=category,
                version=entry.version,
                is_current=False,
                product_type=entry.product_type,
                external_category_id=entry.template.external_category_id,
                source=TEMPLATE_SOURCE,
                raw_schema_key=None,
                attributes=content,
            )
        )
        inserted += 1
        current = (
            await connection.execute(
                select(func.max(table.c.version)).where(
                    table.c.marketplace == marketplace,
                    table.c.category == category,
                    table.c.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
        if (current is None or current < entry.version) and await promote_template(
            connection, marketplace, category, entry.version
        ):
            promoted += 1
    return TemplateCounts(inserted=inserted, unchanged=unchanged, promoted=promoted)


async def load_templates(engine: AsyncEngine, entries: Sequence[TemplateLoad]) -> TemplateCounts:
    async with engine.begin() as connection:
        return await load_template_rows(connection, entries)


# ---- `errors load` (S20 T4; DATABASE.md:546-557) ----
# The file is a JSON list of `ErrorMapIn`. Seeds come from sandbox and observed responses, never
# invented: the committed `app/db/seed_data/error_map.fake.json` holds local `FAKE-` codes only. A
# row is keyed by `mem_key (marketplace, match_on, raw_value, version)`: a stored key with the
# same content is a no-op; one whose only difference is `enabled` flips `enabled` (the one
# mutable column, 0004's `error_map_guard`); any other difference refuses the whole file ("bump
# version", nothing written). Each write is audited with a NULL tenant and only `{enabled}`.

PLAIN_TEXT_MAX: Final = 500
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_ALLOWED_PLACEHOLDERS: Final = frozenset({"{value}", "{field}", "{marketplace}"})
_ERROR_MAP_CONTENT: Final = (
    "error_code",
    "error_class",
    "retry_class",
    "severity",
    "auto_fixable",
    "fix_action",
    "field_hint",
    "plain_text",
    "seller_action",
    "policy_ref",
)


class ErrorMapIn(_Strict):
    marketplace: Marketplace
    match_on: Literal["code", "category"]
    raw_value: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    version: Annotated[int, Field(ge=1)]
    error_code: ErrorCode
    error_class: ErrorClass
    retry_class: RetryClass
    severity: Severity
    auto_fixable: bool
    fix_action: FixAction | None = None
    field_hint: str | None = None
    plain_text: Annotated[str, StringConstraints(min_length=1, max_length=PLAIN_TEXT_MAX)]
    seller_action: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    policy_ref: dict[str, Any] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _checks(self) -> ErrorMapIn:
        if self.auto_fixable != (self.fix_action is not None):
            raise ValueError("auto_fixable must match fix_action (mem_fix)")
        if self.field_hint is not None and not is_valid_field_path(self.field_hint):
            raise ValueError("field_hint must be a legal field path")
        if self.raw_value != self.raw_value.strip():
            raise ValueError("raw_value has surrounding whitespace")
        for token in _PLACEHOLDER.findall(self.plain_text):
            if token not in _ALLOWED_PLACEHOLDERS:
                raise ValueError(
                    "plain_text holds a placeholder other than {value} {field} {marketplace}"
                )
        return self

    def content(self) -> dict[str, Any]:
        dumped = self.model_dump(mode="json")
        return {k: dumped[k] for k in _ERROR_MAP_CONTENT}


class ErrorMapConflict(Exception):  # noqa: N818 - the CLI names it in its exit line
    """A stored key holds other content: the file must bump the version."""

    def __init__(self, row: ErrorMapIn) -> None:
        self.key = f"{row.marketplace.value}/{row.match_on}/{row.raw_value} v{row.version}"
        super().__init__(self.key)


@dataclass(frozen=True, slots=True)
class ErrorMapCounts:
    inserted: int
    unchanged: int
    toggled: int


def load_error_map_file(path: Path) -> list[ErrorMapIn]:
    """The file as `list[ErrorMapIn]`; `FileNotFoundError` and `ValidationError` propagate."""
    return TypeAdapter(list[ErrorMapIn]).validate_json(path.read_bytes())


async def _audit_error_map(
    connection: AsyncConnection,
    row_id: Any,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    await connection.execute(
        text(
            "SELECT audit(NULL, 'system', NULL, :action, 'marketplace_error_map', :id, "
            "CAST(:before AS jsonb), CAST(:after AS jsonb), 'errors load', NULL, NULL, NULL, NULL)"
        ),
        {
            "action": action,
            "id": row_id,
            "before": None if before is None else json.dumps(before),
            "after": json.dumps(after),
        },
    )


async def load_error_map_rows(
    connection: AsyncConnection, rows: Sequence[ErrorMapIn]
) -> ErrorMapCounts:
    """The load on an open transaction (`load_error_map` opens one, as migrator)."""
    await connection.run_sync(_assume_migrator)
    table = MarketplaceErrorMap.__table__
    content_cols = [table.c[name] for name in _ERROR_MAP_CONTENT]
    inserted = unchanged = toggled = 0
    seen: set[tuple[str, str, str, int]] = set()
    for row in rows:
        key = (row.marketplace.value, row.match_on, row.raw_value, row.version)
        if key in seen:
            raise ErrorMapConflict(row)
        seen.add(key)
        stored = (
            await connection.execute(
                select(table.c.id, table.c.enabled, *content_cols)
                .where(
                    table.c.marketplace == key[0],
                    table.c.match_on == key[1],
                    table.c.raw_value == key[2],
                    table.c.version == key[3],
                )
                .with_for_update()
            )
        ).one_or_none()
        content = row.content()
        if stored is None:
            row_id = (
                await connection.execute(
                    insert(MarketplaceErrorMap)
                    .values(
                        marketplace=key[0],
                        match_on=key[1],
                        raw_value=key[2],
                        version=key[3],
                        enabled=row.enabled,
                        **content,
                    )
                    .returning(table.c.id)
                )
            ).scalar_one()
            await _audit_error_map(
                connection, row_id, "marketplace_error_map.loaded", None, {"enabled": row.enabled}
            )
            inserted += 1
            continue
        held = {name: getattr(stored, name) for name in _ERROR_MAP_CONTENT}
        if held != content:
            raise ErrorMapConflict(row)
        if stored.enabled == row.enabled:
            unchanged += 1
            continue
        await connection.execute(
            update(MarketplaceErrorMap)
            .where(MarketplaceErrorMap.id == stored.id)
            .values(enabled=row.enabled)
        )
        await _audit_error_map(
            connection,
            stored.id,
            "marketplace_error_map.enabled_changed",
            {"enabled": stored.enabled},
            {"enabled": row.enabled},
        )
        toggled += 1
    return ErrorMapCounts(inserted=inserted, unchanged=unchanged, toggled=toggled)


async def load_error_map(engine: AsyncEngine, rows: Sequence[ErrorMapIn]) -> ErrorMapCounts:
    async with engine.begin() as connection:
        return await load_error_map_rows(connection, rows)


# ---- `rules load` (DATABASE.md §5.2 rules :496-508; the migrate job, after `dictionary sync`) ----
# The file is a JSON list of `RuleSet` holding platform rules only (`tenant_id` NULL; the
# committed `packages/schemas/fixtures/rules.seed.json` by default). A rule is matched on its fixed
# `id`: the same content (every stored column but `id`, `revision` and the timestamps) is a no-op;
# a difference in `enabled` alone flips it (the one mutable column, 0004's `rules_guard`, which
# takes a new revision; the resolver keeps the highest enabled version, so this is the rollback
# lever); any other difference refuses the whole file ("bump version", nothing written). A new id
# whose `rules_scope_key` a stored platform rule already holds is refused too. Each write is
# audited with a NULL tenant and only `{enabled}`.


def load_rule_sets(path: Path) -> list[RuleSet]:
    """The file as `list[RuleSet]`; `FileNotFoundError` and `ValidationError` propagate."""
    adapter: TypeAdapter[list[RuleSet]] = TypeAdapter(list[RuleSet])
    return adapter.validate_json(path.read_bytes())


def _subject(rule_type: str, rule_def: Mapping[str, Any]) -> str:
    """What S7's `rules_subject_matches` CHECK expects for `rule_type`."""
    if rule_type == "image_requirement":
        return f"{rule_def.get('slot')}:{rule_def.get('check')}"
    key = {
        "claim_requires_attribute": "claim",
        "cross_field": "name",
        "pattern": "name",
        "required_attribute": "attribute",
        "allowed_values": "attribute",
        "conditional_required": "attribute",
        "fact_source": "attribute",
        "fact_backed_attribute": "attribute",
    }.get(rule_type)
    value = rule_def.get(key) if key else None
    return str(value) if value is not None else ""


def platform_rule_rows(rule_sets: Sequence[RuleSet]) -> list[dict[str, Any]]:
    """One `rules` row per rule; `ValueError` for a rule that names a tenant."""
    rows: list[dict[str, Any]] = []
    for rule_set in rule_sets:
        for rule in rule_set.rules:
            data = rule.model_dump(mode="json")
            if data.get("tenant_id") is not None:
                raise ValueError("rules.seed.json holds platform rules only")
            rule_def = data["rule_def"]
            rows.append(
                {
                    "id": uuid.UUID(data["id"]),
                    "tenant_id": None,
                    "marketplace": data["marketplace"],
                    "category": data.get("category"),
                    "field": data["field"],
                    "rule_type": rule_def["type"],
                    "subject": _subject(rule_def["type"], rule_def),
                    "pack": rule_def.get("pack", "core"),
                    "severity": data["severity"],
                    "version": data["version"],
                    "enabled": data["enabled"],
                    "rule_def": rule_def,
                }
            )
    return rows


_RULE_CONTENT: Final = (
    "tenant_id",
    "marketplace",
    "category",
    "field",
    "rule_type",
    "subject",
    "pack",
    "severity",
    "version",
    "rule_def",
)
_SCOPE: Final = ("marketplace", "category", "field", "rule_type", "subject", "pack", "version")


class RuleConflict(Exception):  # noqa: N818 - the CLI names it in its exit line
    """The file cannot load as it is: nothing is written."""

    def __init__(self, rule_id: uuid.UUID, problem: str) -> None:
        self.key = f"rule {rule_id} {problem}"
        super().__init__(self.key)


@dataclass(frozen=True, slots=True)
class RuleCounts:
    inserted: int
    unchanged: int
    toggled: int


async def _audit_rule(
    connection: AsyncConnection,
    rule_id: uuid.UUID,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    await connection.execute(
        text(
            "SELECT audit(NULL, 'system', NULL, :action, 'rules', :id, CAST(:before AS jsonb), "
            "CAST(:after AS jsonb), 'rules load', NULL, NULL, NULL, NULL)"
        ),
        {
            "action": action,
            "id": rule_id,
            "before": None if before is None else json.dumps(before),
            "after": json.dumps(after),
        },
    )


def _scope_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple("" if name == "category" and row[name] is None else row[name] for name in _SCOPE)


async def load_rule_rows(connection: AsyncConnection, rule_sets: Sequence[RuleSet]) -> RuleCounts:
    """The load on an open transaction (`load_rules` opens one, as migrator)."""
    await connection.run_sync(_assume_migrator)
    table = Rule.__table__
    inserted = unchanged = toggled = 0
    seen_ids: set[uuid.UUID] = set()
    seen_scopes: set[tuple[Any, ...]] = set()
    for row in platform_rule_rows(rule_sets):
        rule_id, scope = row["id"], _scope_key(row)
        if rule_id in seen_ids:
            raise RuleConflict(rule_id, "appears twice in the file")
        if scope in seen_scopes:
            raise RuleConflict(rule_id, "repeats another rule's rules_scope_key in the file")
        seen_ids.add(rule_id)
        seen_scopes.add(scope)
        stored = (
            await connection.execute(
                select(table.c.enabled, *(table.c[name] for name in _RULE_CONTENT))
                .where(table.c.id == rule_id)
                .with_for_update()
            )
        ).one_or_none()
        if stored is None:
            twin = (
                await connection.execute(
                    select(table.c.id).where(
                        table.c.tenant_id.is_(None),
                        func.coalesce(table.c.category, "") == scope[1],
                        *(table.c[name] == row[name] for name in _SCOPE if name != "category"),
                    )
                )
            ).first()
            if twin is not None:
                raise RuleConflict(rule_id, "collides with a stored rule on rules_scope_key")
            await connection.execute(insert(Rule).values(**row))
            await _audit_rule(
                connection, rule_id, "rules.loaded", None, {"enabled": row["enabled"]}
            )
            inserted += 1
            continue
        if any(stored._mapping[name] != row[name] for name in _RULE_CONTENT):
            raise RuleConflict(rule_id, "already holds other content; bump version")
        if stored.enabled == row["enabled"]:
            unchanged += 1
            continue
        await connection.execute(
            update(Rule).where(Rule.id == rule_id).values(enabled=row["enabled"])
        )
        await _audit_rule(
            connection,
            rule_id,
            "rules.enabled_changed",
            {"enabled": stored.enabled},
            {"enabled": row["enabled"]},
        )
        toggled += 1
    return RuleCounts(inserted=inserted, unchanged=unchanged, toggled=toggled)


async def load_rules(engine: AsyncEngine, rule_sets: Sequence[RuleSet]) -> RuleCounts:
    async with engine.begin() as connection:
        return await load_rule_rows(connection, rule_sets)
