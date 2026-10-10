# Mutation waivers

The mutation gate (`make mutation`, S26 PLAN D9; nightly leg `mutation`) needs each core module's
score at 80% or more. A module may stay under it only with a row here: the module as
`[tool.mutmut] only_mutate` names it (a package without its `/*`), the release the waiver is for,
the score granted (the run must stay at or above it), why the surviving mutants are acceptable
(for example equivalent mutants), the approving user, and the date (YYYY-MM-DD).
`scripts/ci/mutation_report.py` reads this table; a malformed row fails the run.

| module | release | score | reason | user | date |
|---|---|---|---|---|---|
