-- 0030/01_brand_voice.up.sql (client feedback item 19): the structured brand voice the seller
-- gives in "Tell Kuinn about your brand". The API's `BrandVoice` model owns the shape (audience,
-- price_segment, positioning, differentiators, words_to_use, words_to_avoid, claim_limits,
-- example_copy) and every cap; this CHECK is the backstop: a JSON object of at most 64 KiB. The
-- caps allow about 10.5k characters at up to 4 UTF-8 bytes each, so a full voice in Devanagari
-- or with emoji always fits and the API never reaches this CHECK with a valid voice.
-- Not a fact column (no provenance, no facts_guard): it shapes tone and emphasis only.
ALTER TABLE public.brand_profiles ADD COLUMN brand_voice jsonb;
ALTER TABLE public.brand_profiles ADD CONSTRAINT ck_brand_profiles_brand_voice
  CHECK (brand_voice IS NULL
         OR (jsonb_typeof(brand_voice) = 'object' AND octet_length(brand_voice::text) <= 65536))
  NOT VALID;
ALTER TABLE public.brand_profiles VALIDATE CONSTRAINT ck_brand_profiles_brand_voice;
