# STATE session log: archive

Older session-log entries moved out of `docs/STATE.md`, which keeps the newest 5. Frozen history:
facts here may be stale; `docs/STATE.md` is the current status.

- **2026-09-27 (no-git):**
  - Decided ADRs 0001–0005 (GCP; Python 3.13; PG17; Postgres queue/no Redis; hygiene) and the site as
    the source of truth.
  - Built the agent team, the loop conductor, the session-start hook, `scripts/spec-sync.sh` and the toolchain
    (uv, Python 3.13, pnpm).
  - Mac cleanup (about 7.9 GB).
  - Pending from the user: the iCloud move (on hold).
- **2026-09-26 (no-git):**
  - Read every doc and scaffold file. Verified docs vs scaffold (155 issues confirmed). Evaluated 5
    third-party agent add-on repos (verdict: skip all).
  - Created AGENTS.md, the editor entry file that imports it, STATE.md, SUPERSEDED.md,
    KNOWN-ISSUES.md and the retired v0 rules file in `docs/archive/`.
  - Rewrote README.md. Applied 45 fixes from a 3-lens review of these files.
