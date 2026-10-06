-- 0004/11_li_guard.down.sql
GRANT DELETE ON public.listing_items TO app_rw;
DROP TRIGGER li_guard ON public.listing_items;
DROP FUNCTION public.li_guard();
