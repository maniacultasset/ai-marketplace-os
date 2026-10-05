# Generated contract outputs (CANON.md:43-45): the JSON Schemas, the /api/v1 OpenAPI document and
# (T17) the TypeScript types. Every output is committed and never hand-edited; `make schemas-drift`
# regenerates them and fails on any diff. Needs no database and no network once deps are installed.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c

# The fixed export env (CANON-ADDENDUM.md §13 as corrected by ADR-0004 §3; the CI values, CANON.md:21).
EXPORT_ENV := APP_ENV=local DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/mos_test JWT_SECRET=ci_only_not_a_real_secret_at_least_32_chars

.PHONY: all json openapi
all: json openapi

json:
	uv run --frozen python -m mkos_schemas.export --out jsonschema

openapi:
	$(EXPORT_ENV) uv run --frozen python -m app.openapi_export --out jsonschema/openapi.json
