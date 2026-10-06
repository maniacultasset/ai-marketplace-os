-- 0004/13b_matrix_guard.down.sql
DROP TRIGGER image_slot_guard ON public.images;
DROP FUNCTION public.image_slot_guard();
DROP TRIGGER variant_matrix_complete ON public.variant_matrix_cells;
DROP TRIGGER variant_matrix_complete ON public.products;
DROP FUNCTION public.variant_matrix_complete();
DROP FUNCTION public.variant_matrix_check_child(uuid, uuid);
DROP TRIGGER variant_matrix_guard ON public.variant_matrix_cells;
DROP FUNCTION public.variant_matrix_guard();
