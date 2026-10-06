-- 0004/01_transitions.down.sql
ALTER TABLE public.listings DROP CONSTRAINT ck_listings_last_event;
DROP TABLE public.listing_status_transitions;
