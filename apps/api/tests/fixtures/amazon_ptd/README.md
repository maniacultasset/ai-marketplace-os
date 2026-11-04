Hand-written, trimmed Product Type Definition fixtures for the DW2 tests (decision 9). They
follow the measured envelope keys and the believed attribute names in
`app/adapters/amazon/constants.py` and `app/db/seed_data/platform_templates.json`. They are
never a copied Amazon schema. `KURTA.drift.schema.json` renames `neck_style` and adds a
required `brand`, for the drift and schema-check tests.
