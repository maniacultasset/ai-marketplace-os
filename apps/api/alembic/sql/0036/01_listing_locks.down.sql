-- 0036/01_listing_locks.down.sql (local and CI only)
ALTER TABLE public.listings DROP CONSTRAINT ck_listings_locked_paths;
ALTER TABLE public.listings DROP COLUMN locked_paths;
