-- 0024/02_proposals.down.sql (local and CI only, RULES.md "No prod downgrade")
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.marketplace_error_proposals) THEN
    RAISE EXCEPTION '0024 downgrade refused: marketplace_error_proposals has rows'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$;
DROP TABLE public.marketplace_error_proposals;
DROP FUNCTION public.marketplace_error_proposals_guard();
