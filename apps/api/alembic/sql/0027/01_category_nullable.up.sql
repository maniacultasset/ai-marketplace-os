-- 0027/01_category_nullable.up.sql (SI14, D3 approved by the user 2026-11-09): a product may be
-- created before its category is known; the classifier suggests one and the seller confirms it.
-- DROP NOT NULL is catalog-only on PostgreSQL 17. 0001's ck_products_typed_facts_sourced needs
-- `provenance ? 'category'` on every row, so it is rebuilt with the category sourced only when
-- set (the other typed facts' rule); every other term is 0001's.
ALTER TABLE public.products ALTER COLUMN category DROP NOT NULL;
ALTER TABLE public.products DROP CONSTRAINT ck_products_typed_facts_sourced;
ALTER TABLE public.products ADD CONSTRAINT ck_products_typed_facts_sourced CHECK (
        provenance ? 'title' AND (category IS NULL OR provenance ? 'category')
        AND (mrp IS NULL OR provenance ? 'mrp')
        AND (selling_price IS NULL OR provenance ? 'selling_price')
        AND (tax_rate IS NULL OR provenance ? 'tax_rate')
        AND (hsn_code IS NULL OR provenance ? 'hsn_code')
        AND (country_of_origin IS NULL OR provenance ? 'country_of_origin')
        AND (generic_name IS NULL OR provenance ? 'generic_name')
        AND (net_quantity IS NULL OR provenance ? 'net_quantity')
        AND (identifier_status IS NULL OR provenance ? 'identifier')
        AND (package_length_cm IS NULL OR provenance ? 'package_dimensions')
        AND (package_weight_g IS NULL OR provenance ? 'package_weight')
        AND (product_weight_g IS NULL OR provenance ? 'product_weight')
        AND (manufacturer_party_id IS NULL OR provenance ? 'manufacturer')
        AND (marketer_party_id IS NULL OR provenance ? 'marketer')
        AND (packer_party_id IS NULL OR provenance ? 'packer')
        AND (importer_party_id IS NULL OR provenance ? 'importer')
        AND (consumer_care_party_id IS NULL OR provenance ? 'consumer_care')
        AND (brand_name IS NULL OR provenance ? 'brand_name')
        AND (sub_category IS NULL OR provenance ? 'sub_category')
        AND (product_type IS NULL OR provenance ? 'product_type')
        AND (gender IS NULL OR provenance ? 'gender')
      )
  NOT VALID;
ALTER TABLE public.products VALIDATE CONSTRAINT ck_products_typed_facts_sourced;
