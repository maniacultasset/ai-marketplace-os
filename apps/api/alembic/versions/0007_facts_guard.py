"""facts_guard: the fact provenance backstop on products, colourways and brand profiles

DATABASE.md rev 3 §5.6 `facts_guard()` (steps 1-3) and :1797; CANON.md ledger row 0007 (S13b).
BEFORE INSERT OR UPDATE OF `attributes`, `provenance` and the typed fact columns on `products`,
`product_colourways` and `brand_profiles`, SECURITY INVOKER with a pinned search_path:

1. The row's level: `brand` (brand_profiles); `colour` (colourways; the category is the style's,
   read FOR SHARE); `variant`, `style` or `standalone` (products, by parent and theme).
2. Each `attributes` key whose envelope changed (OLD vs NEW per key; every key when the row's
   level or category changed, else an untouched key is never
   re-checked, so a dictionary change cannot block an unrelated write): the key is known, enabled
   and `storage = 'attributes'` (PRV02); the level is in `owner_levels` (PRV03); the source is in
   `allowed_sources`, and never `ai` for a key that is critical or carries `critical_override`
   for the row's category (PRV01).
3. The same for each changed `provenance` key, against `storage = 'column'`.

A malformed `attributes` or `provenance` map is left to the 0001 CHECK (`fact_map_valid`), which
refuses it with its own constraint name. PRV04 (a typed value outliving its source) and PRV05 (party roles) are not in this revision. The
guard reads `attribute_definitions` at write time, so every environment runs `app.cli dictionary
sync` after `upgrade` (DATABASE.md:1785). CREATE TRIGGER takes SHARE ROW EXCLUSIVE briefly and
rewrites nothing. Messages carry no key or value; the API maps the SQLSTATE (PRV01
FACT_SOURCE_FORBIDDEN, PRV02 UNKNOWN_FACT_KEY, PRV03 FACT_LEVEL_INVALID).

Revision ID: 0007_facts_guard
Revises: 0006_oauth_pending
Create Date: 2026-10-07

"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_facts_guard"
down_revision: str | Sequence[str] | None = "0006_oauth_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The typed fact columns (DATABASE.md:1104-1113, :1123-1130; the `attribute_columns` pairs).
PRODUCT_FACT_COLUMNS: Final[tuple[str, ...]] = (
    "title",
    "category",
    "brand_name",
    "style_number",
    "sub_category",
    "product_type",
    "gender",
    "stock_ref",
    "mrp",
    "selling_price",
    "tax_rate",
    "hsn_code",
    "country_of_origin",
    "generic_name",
    "net_quantity",
    "net_quantity_unit",
    "manufacturer_party_id",
    "marketer_party_id",
    "packer_party_id",
    "importer_party_id",
    "consumer_care_party_id",
    "identifier_type",
    "identifier_value",
    "identifier_status",
    "package_length_cm",
    "package_breadth_cm",
    "package_height_cm",
    "package_weight_g",
    "product_weight_g",
)
BRAND_FACT_COLUMNS: Final[tuple[str, ...]] = (
    "brand_names",
    "brand_owner_type",
    "website_url",
    "brand_description",
    "logo_storage_key",
    "legal_entity_party_id",
    "default_manufacturer_party_id",
    "default_marketer_party_id",
    "default_packer_party_id",
    "default_importer_party_id",
    "consumer_care_party_id",
    "default_country_of_origin",
    "content_locale",
)
TRIGGERS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    # the level columns too (category is a fact column already): a level change re-checks every
    # key (S13b review #1)
    (
        "products",
        (
            "attributes",
            "provenance",
            *PRODUCT_FACT_COLUMNS,
            "parent_product_id",
            "variation_theme",
        ),
    ),
    ("product_colourways", ("attributes",)),
    ("brand_profiles", ("provenance", *BRAND_FACT_COLUMNS)),
)

CHECK_FUNCTION: Final = """
CREATE FUNCTION public.facts_guard_check(
  p_key text, p_fact jsonb, p_storage text, p_level text, p_category text
) RETURNS void
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  d record;
  src text;
BEGIN
  SELECT a.storage, a.enabled, a.owner_levels, a.allowed_sources, a.is_critical INTO d
    FROM public.attribute_definitions a
   WHERE a.key = p_key;
  IF NOT FOUND OR NOT d.enabled OR d.storage <> p_storage THEN
    RAISE EXCEPTION 'an unknown, disabled or wrongly stored fact key' USING ERRCODE = 'PRV02';
  END IF;
  IF NOT (p_level = ANY (d.owner_levels)) THEN
    RAISE EXCEPTION 'a fact key outside its owner levels' USING ERRCODE = 'PRV03';
  END IF;
  src := p_fact ->> 'source';
  IF src IS NULL OR NOT (src = ANY (d.allowed_sources)) THEN
    RAISE EXCEPTION 'a fact source the key does not allow' USING ERRCODE = 'PRV01';
  END IF;
  IF src = 'ai' AND (d.is_critical OR EXISTS (
       SELECT 1 FROM public.category_attributes ca
        WHERE ca.category = p_category AND ca.attribute_key = p_key
          AND ca.enabled AND ca.critical_override)) THEN
    RAISE EXCEPTION 'an ai source on a critical fact key' USING ERRCODE = 'PRV01';
  END IF;
END
$$
"""

GUARD_FUNCTION: Final = """
CREATE FUNCTION public.facts_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  lvl text;
  cat text;
  k text;
  fact jsonb;
  recheck boolean := false;  -- the level or category moved: an untouched key may now be wrong
BEGIN
  IF TG_TABLE_NAME = 'brand_profiles' THEN
    lvl := 'brand';
  ELSIF TG_TABLE_NAME = 'product_colourways' THEN
    lvl := 'colour';
    SELECT p.category INTO cat FROM public.products p
     WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.style_product_id FOR SHARE;
  ELSE
    cat := NEW.category;
    lvl := CASE WHEN NEW.parent_product_id IS NOT NULL THEN 'variant'
                WHEN NEW.variation_theme IS NOT NULL THEN 'style'
                ELSE 'standalone' END;
    IF TG_OP = 'UPDATE' THEN
      recheck := OLD.category IS DISTINCT FROM NEW.category
        OR (OLD.parent_product_id IS NULL) <> (NEW.parent_product_id IS NULL)
        OR (OLD.variation_theme IS NULL) <> (NEW.variation_theme IS NULL);
    END IF;
  END IF;
  -- a malformed map is the CHECK's to refuse (fact_map_valid, 0001): BEFORE triggers run first.
  -- Nested IFs: plpgsql may evaluate both operands of AND, and brand_profiles has no attributes.
  IF TG_TABLE_NAME <> 'brand_profiles' THEN
    IF public.fact_map_valid(NEW.attributes, true) THEN
      FOR k, fact IN SELECT e.key, e.value FROM jsonb_each(NEW.attributes) e LOOP
        CONTINUE WHEN TG_OP = 'UPDATE' AND NOT recheck
          AND (OLD.attributes -> k) IS NOT DISTINCT FROM fact;
        PERFORM public.facts_guard_check(k, fact, 'attributes', lvl, cat);
      END LOOP;
    END IF;
  END IF;
  IF TG_TABLE_NAME <> 'product_colourways' THEN
    IF public.fact_map_valid(NEW.provenance, false) THEN
      FOR k, fact IN SELECT e.key, e.value FROM jsonb_each(NEW.provenance) e LOOP
        CONTINUE WHEN TG_OP = 'UPDATE' AND NOT recheck
          AND (OLD.provenance -> k) IS NOT DISTINCT FROM fact;
        PERFORM public.facts_guard_check(k, fact, 'column', lvl, cat);
      END LOOP;
    END IF;
  END IF;
  RETURN NEW;
END
$$
"""


def _run_function(statement: str) -> None:
    # `:` would read as a bind parameter in sa.text, as in 0004 and 0005.
    op.execute(sa.text(statement.replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_function(CHECK_FUNCTION)
    _run_function(GUARD_FUNCTION)
    # called from the trigger as the writing role (SECURITY INVOKER)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.facts_guard_check(text, jsonb, text, text, text) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.facts_guard_check(text, jsonb, text, text, text) "
        "TO app_rw"
    )
    for table, columns in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER facts_guard BEFORE INSERT OR UPDATE OF {', '.join(columns)} "
            f"ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.facts_guard()"
        )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for table, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER facts_guard ON public.{table}")
    op.execute("DROP FUNCTION public.facts_guard()")
    op.execute("DROP FUNCTION public.facts_guard_check(text, jsonb, text, text, text)")
