"""Export Pydantic models to JSON Schema files."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from .agent import AgentRunRecord
from .auth import JWTClaims
from .errors import ErrorReport
from .images import ImageReport
from .imports import ImportReport
from .jobs import (
    BrandChunkDTO,
    CategoryTemplate,
    GenerateListingRequest,
    GenerationContext,
    GenerationResult,
    JobAccepted,
    JobEnvelope,
    ModelCredentials,
)
from .listing import CanonicalListing
from .publish import ListingResult
from .rules import RuleSet
from .validation import ValidationResult, ValidationSummary

SCHEMA_ID_BASE: Final = "https://schemas.mkos.internal/"

MODELS: Final[tuple[type[BaseModel], ...]] = (
    CanonicalListing,
    ErrorReport,
    ImageReport,
    AgentRunRecord,
    ValidationResult,
    ValidationSummary,
    GenerateListingRequest,
    JobAccepted,
    JobEnvelope,
    GenerationContext,
    GenerationResult,
    BrandChunkDTO,
    CategoryTemplate,
    ModelCredentials,
    ListingResult,
    ImportReport,
    JWTClaims,
    RuleSet,
)


def export(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in MODELS:
        schema = model.model_json_schema(mode="serialization", ref_template="#/$defs/{model}")
        schema["$id"] = f"{SCHEMA_ID_BASE}{model.__name__}.schema.json"
        schema["title"] = model.__name__
        path = out / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    for stale in out.glob("*.schema.json"):
        if stale not in written:
            stale.unlink()
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mkos_schemas.export")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    export(Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
