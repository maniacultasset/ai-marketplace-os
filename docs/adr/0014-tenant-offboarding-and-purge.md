# ADR-0014: Tenant offboarding, the two-phase purge and user erasure

- **Status:** proposed
- **Date:** 2026-10-06
- **Deciders:** Tech Lead (counsel confirms retention, DATABASE.md open decision 4)

## Context

`audit_log` is append-only (no UPDATE, no DELETE for `app_rw`, a trigger for everyone else), and the
Canon had it cascade from `tenants`, so as designed a tenant could never be deleted and there was no
DPDP erasure path (KNOWN-ISSUES.md, "audit_log ON DELETE CASCADE"). DATABASE.md §9 ("Tenant lifecycle
and DPDP erasure", "Pruning") and §5.1 (tenants "Privileges") propose the lifecycle, the purge and the
retention values; revision `0004_integrity` builds them.

## Decision

A tenant moves `trial | active | suspended | offboarded` only through two migrator-owned SECURITY
DEFINER functions with a pinned `search_path`: `set_tenant_lifecycle(tenant, status, reason)` (the app
may only offboard its own trial or active tenant; staff, logged in as `migrator`, make every other
move; a suspension revokes the tenant's refresh tokens) and `expire_trials()`. Offboarding stamps
`offboarded_at` and `purge_after` = 30 days. The migrator-only `purge_tenant(t, 'data')` then deletes
every tenant table except `tenants`, `tenant_memberships` and `audit_log`, children first (the order is
`purge_tenant_order()`), nulls `actor_email` and `ip` in the tenant's audit rows and pseudonymises users
with no other membership; `purge_tenant(t, 'final')`, after audit retention, deletes the audit rows,
the tenant and the users left without a membership. Each phase writes a `tenant.purged` tombstone with
a NULL tenant. `audit_log.tenant_id` is nullable with NO ACTION, and the append-only triggers allow
these deletes only when `session_user = 'migrator'` and `app.purge_tenant` names the row's tenant. User
erasure is the migrator-only `redact_user(user)`. Pruning is migrator-only: `prune_audit_log` (24-month
floor), `prune_append_only` (agent and validation runs, 13 months) and `prune_closed` (closed issues and
terminal tasks, 13 months), each under `app.prune_table`. Retention: audit 24 months (never under 12),
30-day offboarding grace, backups expire within 35 days.

## Consequences

- Erasure becomes possible without weakening append-only: no app role can delete audit rows, and a
  superuser cannot either without the migrator login.
- The Cloud Run job that runs the purge must log in as `migrator`; the storage prefix is deleted by that
  job, not by the database.
- Redacting the last active owner of a tenant fails at commit (the last-owner guard) until staff appoint
  another owner.
- The retention values stay proposed until counsel confirms them (open decision 4).
