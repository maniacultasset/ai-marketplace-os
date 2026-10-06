-- scripts/db/rls_emergency_disable.sql: psql only (\if, :'var'); the emergency switch that replaces
-- the Canon's "0005 behind a settings flag" (DATABASE.md §11.2 row 6), kept for the week after
-- 0005_rls first reaches an environment. It turns row-level security off on every table that has
-- it, read from the catalog (never a list), so a later revision's table is covered too.
--
-- Run it as migrator, the owner of every table: session_user must be 'migrator' (log in as
-- migrator, or SET SESSION AUTHORIZATION migrator first), and name the staff member:
--   psql "<migrator DSN>" -v ON_ERROR_STOP=1 -X -v operator_email=<staff email> \
--     -f scripts/db/rls_emergency_disable.sql
-- In one transaction it writes a staff tombstone to audit_log (NULL tenant, the operator's email,
-- the table count as the reason), then NO FORCE + DISABLE ROW LEVEL SECURITY per table. Any
-- failure rolls back everything. Recovery puts every policy back:
--   alembic downgrade 0004_integrity && alembic upgrade head
\set ON_ERROR_STOP on
\set VERBOSITY terse
\if :{?operator_email}
\else
DO $$ BEGIN RAISE EXCEPTION 'rls_emergency_disable: pass -v operator_email=<staff email>; nothing was changed'; END $$;
\endif
BEGIN;
SET LOCAL lock_timeout = '5s';
SELECT set_config('mkos.rls_operator_email', :'operator_email', true) IS NOT NULL AS operator_set \gset
DO $$
DECLARE
  operator text := nullif(btrim(current_setting('mkos.rls_operator_email', true)), '');
  rel regclass;
  tables regclass[];
BEGIN
  IF session_user <> 'migrator' THEN
    RAISE EXCEPTION 'rls_emergency_disable: run as migrator (session_user is %); nothing was changed',
      session_user;
  END IF;
  IF operator IS NULL THEN
    RAISE EXCEPTION 'rls_emergency_disable: operator_email is empty; nothing was changed';
  END IF;
  SELECT coalesce(array_agg(c.oid::regclass ORDER BY c.relname), '{}') INTO tables
    FROM pg_catalog.pg_class c
   WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')
     AND (c.relrowsecurity OR c.relforcerowsecurity);
  INSERT INTO public.audit_log (tenant_id, actor_type, actor_email, action, entity_type, reason)
  VALUES (NULL, 'staff', operator, 'rls.emergency_disabled', 'tenant',
          format('row-level security disabled on %s tables', cardinality(tables)));
  FOREACH rel IN ARRAY tables LOOP
    EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', rel);
    EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', rel);
  END LOOP;
  RAISE NOTICE 'rls_emergency_disable: row-level security disabled on % tables', cardinality(tables);
END
$$;
COMMIT;
