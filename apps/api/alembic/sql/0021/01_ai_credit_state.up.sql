-- 0021/01_ai_credit_state.up.sql (SI10): the AI provider pause and the last OpenRouter /key check.
-- One platform row, app-written under forced RLS; the clear is guarded here, not in Python.

CREATE TABLE public.ai_credit_state (
  id smallint NOT NULL,
  paused_reason text,
  paused_at timestamptz,
  limit_usd numeric(12, 6),
  usage_usd numeric(12, 6),
  remaining_usd numeric(12, 6),
  checked_at timestamptz,
  resumed_at timestamptz,
  resumed_by_user_id uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_ai_credit_state PRIMARY KEY (id),
  CONSTRAINT ck_ai_credit_state_one_row CHECK (id = 1),
  CONSTRAINT ck_ai_credit_state_reason
    CHECK (paused_reason IN ('credit_exhausted', 'key_invalid')),
  CONSTRAINT ck_ai_credit_state_paused CHECK ((paused_reason IS NULL) = (paused_at IS NULL)),
  CONSTRAINT fk_ai_credit_state_resumed_by_user_id_users FOREIGN KEY (resumed_by_user_id)
    REFERENCES public.users (id) ON DELETE SET NULL
);

INSERT INTO public.ai_credit_state (id) VALUES (1);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.ai_credit_state
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- The guard: a set reason is cleared or changed, and resumed_* written, only by migrator, a
-- superuser or a platform admin (by app.user_id), and an admin records itself as the resumer.
CREATE FUNCTION public.ai_credit_state_guard() RETURNS trigger
  LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF (OLD.paused_reason IS NOT NULL AND NEW.paused_reason IS DISTINCT FROM OLD.paused_reason)
     OR NEW.resumed_at IS DISTINCT FROM OLD.resumed_at
     OR NEW.resumed_by_user_id IS DISTINCT FROM OLD.resumed_by_user_id THEN
    IF current_user = 'migrator'
       OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r
                     WHERE r.rolname = current_user), false) THEN
      RETURN NEW;
    END IF;
    IF NOT (SELECT public.app_is_platform_admin()) THEN
      RAISE EXCEPTION 'the AI pause is cleared only by staff' USING ERRCODE = 'GRD01';
    END IF;
    IF NEW.resumed_by_user_id IS DISTINCT FROM OLD.resumed_by_user_id
       AND NEW.resumed_by_user_id IS DISTINCT FROM (SELECT public.app_user_id()) THEN
      RAISE EXCEPTION 'resumed_by_user_id must be the acting admin' USING ERRCODE = 'GRD01';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER ai_credit_state_guard BEFORE UPDATE ON public.ai_credit_state
  FOR EACH ROW EXECUTE FUNCTION public.ai_credit_state_guard();

-- The worker's automatic clear (owner migrator, so the guard lets it through). key_invalid
-- clears on any successful check; credit_exhausted only for a limited key whose remaining
-- credit is at least the floor (USD 0.50, ai_credit.CREDIT_FLOOR_USD) and rose above the
-- stored value. An unlimited key (NULL) shows no balance, so it never auto-clears.
CREATE FUNCTION public.app_ai_credit_auto_clear(new_remaining numeric) RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  cur public.ai_credit_state%ROWTYPE;
BEGIN
  SELECT * INTO cur FROM public.ai_credit_state WHERE id = 1 FOR UPDATE;
  IF NOT FOUND OR cur.paused_reason IS NULL THEN
    RETURN false;
  END IF;
  IF cur.paused_reason = 'credit_exhausted' AND NOT (
       new_remaining IS NOT NULL
       AND new_remaining >= 0.50
       AND (cur.remaining_usd IS NULL OR new_remaining > cur.remaining_usd)) THEN
    RETURN false;
  END IF;
  UPDATE public.ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1;
  RETURN true;
END
$$;

REVOKE ALL ON FUNCTION public.app_ai_credit_auto_clear(numeric) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.app_ai_credit_auto_clear(numeric) TO app_rw;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.ai_credit_state FROM app_rw;
GRANT SELECT ON public.ai_credit_state TO app_rw, app_ro, app_platform_ro;
GRANT UPDATE (paused_reason, paused_at, limit_usd, usage_usd, remaining_usd, checked_at,
              resumed_at, resumed_by_user_id, updated_at)
  ON public.ai_credit_state TO app_rw;

ALTER TABLE public.ai_credit_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_credit_state FORCE ROW LEVEL SECURITY;
-- A platform boolean and four numbers, no tenant data: app_rw reads it in any session.
CREATE POLICY acs_read ON public.ai_credit_state FOR SELECT TO app_rw USING (true);
-- The trigger above is the guard on what an update may change.
CREATE POLICY acs_write ON public.ai_credit_state FOR UPDATE TO app_rw
  USING (true) WITH CHECK (true);
CREATE POLICY t_platform ON public.ai_credit_state FOR SELECT TO app_ro, app_platform_ro
  USING (current_user IN ('app_platform_ro', 'app_ro'));
CREATE POLICY t_migrator ON public.ai_credit_state TO migrator USING (true) WITH CHECK (true);
