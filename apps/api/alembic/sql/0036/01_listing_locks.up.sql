-- 0036/01_listing_locks.up.sql (client feedback 14, 11 Nov 2026): the listing paths the seller
-- edited and locked (`services/sections.py`): `content.title`, `content.bullets`,
-- `content.description`, `keywords.search_terms`, `keywords.hidden_terms` or `attributes.<key>`.
-- No regenerate changes a locked path. On the listing, not the version, so a lock carries across
-- versions (versions stay immutable). A constant default: no table rewrite (PostgreSQL 11+).
-- (The read-only roles hold table-level SELECT on listings, so no column grant is needed.)
ALTER TABLE public.listings ADD COLUMN locked_paths text[] NOT NULL DEFAULT '{}'::text[];
ALTER TABLE public.listings
  ADD CONSTRAINT ck_listings_locked_paths CHECK (cardinality(locked_paths) <= 256) NOT VALID;
ALTER TABLE public.listings VALIDATE CONSTRAINT ck_listings_locked_paths;
