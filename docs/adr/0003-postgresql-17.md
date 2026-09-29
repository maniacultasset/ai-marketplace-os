# ADR-0003: PostgreSQL 17 + pgvector floor

- **Status:** accepted (user, in chat, 2026-09-27)
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Evidence:** `docs/research/stack-2026-09.md`

## Context

The Canon pins PostgreSQL 16 (compose image `pgvector/pgvector:pg16`), and 0002b requires
pgvector ≥ 0.8.
- PG17 is mature (17.11), supported to 2029-11, and available with pgvector 0.8.5 on every serious
  managed host.
- PG17's vacuum improvements help a Postgres-backed job queue (ADR-0004).
- PG18 would add a year of support, but it's newer and not yet offered everywhere.
- pgvector versions before 0.8.2 carry CVE-2026-3172.

## Decision

- **PostgreSQL 17** in every environment.
- **pgvector extension version ≥ 0.8.2** (prefer ≥ 0.8.4):
  - migration 0002b fails fast if `extversion < 0.8.2`;
  - local and CI pin the image `pgvector/pgvector:0.8.5-pg17` (never a floating tag).
- Python client `pgvector>=0.5,<0.6`. It returns lists or `Vector`, not numpy arrays. That only
  affects our own embedding code; embeddings never cross the AI seam.
- The managed-DB edition and flags are set in the hosting ADR (0001). If the host is Cloud SQL,
  create it explicitly with `POSTGRES_17` and the Enterprise edition; the default would be PG18 on
  Enterprise Plus.

## Consequences

- One more year of support than 16, at no cost now.
- The Canon's compose image, the 0002b pgvector requirement and the stack text change. See the
  CANON.md amendments.
