# ADR-0007: Apparel variant families

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written. It matches the BRD (§7 Variants).
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Detail:** `docs/architecture/DATABASE.md`, "Catalog" (`products`)

## Context

- The first customers are Indian D2C brands, many of them apparel, sold as size × colour families.
- The BRD requires a parent SKU / style ID (L407), size charts and parent-child links (L125). It says
  to "record parent-child relationships separately from marketplace-specific variation rules" (L638).
- The build-plan DDL and Canon 0001 have a flat `products` table with no family model.
- Invariant 4: the canonical listing holds no marketplace-specific fields.

## Decision

1. **The family lives on `products`:**
   - `parent_product_id` is a self-reference with a composite, tenant-safe FK.
   - A parent carries a neutral `variation_theme` (e.g. `size_color`).
   - A child carries `variant_attributes` jsonb (e.g. `{"size":"M","color":"navy"}`).
2. **CSV import gains a `parent_sku` column.** It resolves to `parent_product_id` in the same tenant.
3. **Adapters own the mapping** from the neutral theme to Amazon's `variation_theme` or Flipkart's
   grouping. They publish the parent/child family as one unit.
4. **Constraints:**
   - A product is not its own parent.
   - Only parents carry a theme.
   - Children's attribute keys equal the parent theme's axes.
   - A normalised `variant_key` is unique per parent, so `M/Navy` and `m/navy` collide at import,
     not at the marketplace.

## Consequences

- Adds columns and checks to `products` (a Canon 0001 extension, "Amendments by ADR").
- `CategoryTemplate` gains `variation_themes`. That is a MINOR `mkos-schemas` change and needs the
  three leads.
- Publishing a family is one idempotent adapter operation, with partial retries per child
  (invariant 10).
- The rules engine validates family completeness as data rules, not code (invariant 2).
