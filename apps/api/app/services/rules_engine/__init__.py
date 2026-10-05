"""The Rules Engine: a pure, deterministic function over a listing and a resolved RuleSet.

Invariant 2 (docs/RULES.md §1): deterministic code, never a model call, and the final arbiter.
The package imports no database, web, clock, randomness or I/O module; the
``rules-engine-is-pure`` import contract and ``test_rules_engine_import_allowlist`` enforce it.
"""
