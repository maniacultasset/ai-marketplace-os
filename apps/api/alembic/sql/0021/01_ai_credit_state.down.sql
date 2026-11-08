-- 0021/01_ai_credit_state.down.sql (local and CI only, RULES.md "No prod downgrade")
DROP FUNCTION public.app_ai_credit_auto_clear(numeric);
DROP TABLE public.ai_credit_state;
DROP FUNCTION public.ai_credit_state_guard();
