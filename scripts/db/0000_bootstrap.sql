-- scripts/db/0000_bootstrap.sql — idempotent and additive only; NOT an Alembic revision.
-- Runs as the local superuser, or as a CREATEROLE non-superuser that owns the database
-- (Cloud SQL `postgres`). No passwords here: see scripts/db/0000_role_passwords.sql.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator')
  THEN CREATE ROLE migrator LOGIN; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw')
  THEN CREATE ROLE app_rw LOGIN; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro')
  THEN CREATE ROLE app_ro LOGIN; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_platform_ro')
  THEN CREATE ROLE app_platform_ro NOLOGIN; END IF; END $$;
-- A non-superuser needs SET (not INHERIT) on migrator for the owner change and the block below.
DO $$
BEGIN
  IF current_user <> 'migrator'
     AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    EXECUTE format('GRANT migrator TO %I WITH INHERIT FALSE, SET TRUE', current_user);
  END IF;
END $$;
ALTER SCHEMA public OWNER TO migrator;
SET ROLE migrator;
GRANT USAGE ON SCHEMA public TO app_rw, app_ro, app_platform_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
GRANT app_platform_ro TO app_rw WITH INHERIT FALSE, SET TRUE;
GRANT app_platform_ro TO app_ro WITH INHERIT TRUE;
ALTER ROLE migrator SET lock_timeout = '5s';
ALTER ROLE app_rw SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE app_ro SET default_transaction_read_only = on;
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone TO ''UTC''', current_database());
END $$;
