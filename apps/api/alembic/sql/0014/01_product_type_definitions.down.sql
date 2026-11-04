-- 0014/01_product_type_definitions.down.sql (local and CI only; the rows are refreshable)
DROP TRIGGER product_type_definitions_guard ON public.product_type_definitions;
DROP FUNCTION public.product_type_definitions_guard();
DROP TABLE public.product_type_definitions;
