-- 0004/12_issues_tasks_guards.down.sql
GRANT DELETE ON public.listing_issues, public.human_tasks TO app_rw;
DROP TRIGGER human_tasks_guard ON public.human_tasks;
DROP FUNCTION public.human_tasks_guard();
DROP TRIGGER listing_issues_guard ON public.listing_issues;
DROP FUNCTION public.listing_issues_guard();
