-- 0027/01_category_nullable.down.sql (local and CI only, RULES.md "No prod downgrade")
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.products WHERE category IS NULL) THEN
    RAISE EXCEPTION '0027 downgrade refused: products without a category exist' USING ERRCODE = 'GRD01';
  END IF;
END
$$;
-- 0001's CHECK
ALTER TABLE public.products DROP CONSTRAINT ck_products_typed_facts_sourced;
ALTER TABLE public.products ADD CONSTRAINT ck_products_typed_facts_sourced CHECK (
        provenance ? 'title' AND provenance ? 'category'
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
      );
ALTER TABLE public.products ALTER COLUMN category SET NOT NULL;
