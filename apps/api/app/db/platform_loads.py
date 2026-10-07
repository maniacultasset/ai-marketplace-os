"""The migrator's platform loaders (`python -m app.cli dictionary sync`, `templates load`; S13 B5,
B7; DATABASE.md §5.6 :1152-1179, §5.2 :543, §8.2). The SQL lives here, inside `app/db` (repo rule
R4); the CLI only orchestrates. Every loader runs as `migrator` in one transaction: the platform
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
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import Connection, literal_column, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models.platform import AttributeColumn, AttributeDefinition, CategoryAttribute

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
