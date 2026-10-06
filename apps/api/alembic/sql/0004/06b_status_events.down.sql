-- 0004/06b_status_events.down.sql
DROP TRIGGER listings_status_guard_events ON public.listings;
DROP FUNCTION public.listings_status_events();
