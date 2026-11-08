Hand-written, trimmed Product Type Definition fixtures for the DW2 tests (decision 9). They
follow the measured envelope keys and the believed attribute names in
`app/adapters/amazon/constants.py` and `app/db/seed_data/platform_templates.json`. They are
never a copied Amazon schema. `KURTA.drift.schema.json` renames `neck_style` and adds a
required `item_type_keyword` (nothing sends it), for the drift and schema-check tests.

SI2: `KURTA.listing.schema.json` and `SHIRT.listing.schema.json` are the `LISTING` definitions (the
PRODUCT_ONLY ones plus a required `purchasable_offer`, `condition_type` and
`manufacturer_contact_information`); the fake serves them for `requirements=LISTING`. Since SI2 the
PRODUCT_ONLY fixtures carry `material` (the fabric enum), a text `fabric_type` (the composition),
`manufacturer` and an enum `country_of_origin`, as the templates map them.

SI8: every KURTA, SHIRT and LAMP fixture declares `brand` (text with `language_tag`, required in the
apparel ones), `model_number` and `part_number` (text, no `language_tag`); the KURTA and SHIRT ones
also declare `target_gender` (enum `female|male|unisex`, no `language_tag`, required in `LISTING`),
`department` and `age_range_description` (text enums). These names, shapes and values are
**believed**; DL-A (the real LISTING definitions stored by `templates refresh`, then
`templates diff`) replaces them.
