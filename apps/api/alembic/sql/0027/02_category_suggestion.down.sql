-- 0027/02_category_suggestion.down.sql (local and CI only)
ALTER TABLE public.products DROP CONSTRAINT ck_products_category_suggestion;
DROP FUNCTION public.category_suggestion_valid(jsonb);
ALTER TABLE public.products DROP COLUMN category_suggestion;
