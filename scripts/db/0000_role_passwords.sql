-- scripts/db/0000_role_passwords.sql: psql only (\getenv, \if, backquotes); never run by psycopg.
-- Sets role passwords from SCRAM-SHA-256 verifiers made by scripts/db/scram_verifier.py and held in
-- the operator's environment as APP_RW_SCRAM, APP_RO_SCRAM and MIGRATOR_SCRAM; unset or empty skips
-- that role. The shell checks every value first, reading the environment (never argv), and one bad
-- value stops the run before any value is sent. Then the server must not log statements or keep
-- their text (pg_stat_statements with utility tracking: set the Cloud SQL flag
-- pg_stat_statements.track_utility=off first), and every target role must exist and be alterable,
-- so no ALTER ROLE carrying a verifier is logged, stored or fails.
\set ON_ERROR_STOP on
\set VERBOSITY terse
\set scram_ok `for v in "${APP_RW_SCRAM-}" "${APP_RO_SCRAM-}" "${MIGRATOR_SCRAM-}"; do case "$v" in '') ;; *[!A-Za-z0-9+/=:\$-]*) echo false; exit 0 ;; 'SCRAM-SHA-256$'[0-9]*:*'$'*:*) ;; *) echo false; exit 0 ;; esac; done; echo true`
\if :scram_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'a *_SCRAM value is not a SCRAM-SHA-256 verifier; no password was set'; END $$;
\endif
\set any_set `for v in "${APP_RW_SCRAM-}" "${APP_RO_SCRAM-}" "${MIGRATOR_SCRAM-}"; do test -n "$v" && { echo true; exit 0; }; done; echo false`
\if :any_set
SELECT current_setting('log_statement') = 'none'
   AND current_setting('log_min_duration_statement') = '-1'
   AND current_setting('log_min_duration_sample') = '-1'
   AND current_setting('log_transaction_sample_rate') = '0'
   AND current_setting('debug_print_parse') = 'off'
   AND current_setting('debug_print_rewritten') = 'off'
   AND current_setting('debug_print_plan') = 'off'
   AND coalesce(current_setting('pgaudit.log', true), 'none') = 'none'
   AND coalesce(current_setting('pg_stat_statements.track_utility', true), 'off') = 'off'
   AND (SELECT count(*) FROM pg_roles WHERE rolname IN ('app_rw', 'app_ro', 'migrator')) = 3
   AND (SELECT r.rolsuper OR (r.rolcreaterole AND (SELECT bool_and(pg_has_role(current_user, x.oid, 'MEMBER WITH ADMIN OPTION'))
          FROM pg_roles x WHERE x.rolname IN ('app_rw', 'app_ro', 'migrator')))
        FROM pg_roles r WHERE r.rolname = current_user) AS pw_safe \gset
\if :pw_safe
\else
DO $$ BEGIN RAISE EXCEPTION 'the server logs or keeps statements, or a target role is missing or not alterable; no password was set'; END $$;
\endif
\endif
\getenv app_rw_scram APP_RW_SCRAM
\set app_rw_set `test -n "${APP_RW_SCRAM-}" && echo true || echo false`
\if :app_rw_set
ALTER ROLE app_rw PASSWORD :'app_rw_scram';
\endif
\getenv app_ro_scram APP_RO_SCRAM
\set app_ro_set `test -n "${APP_RO_SCRAM-}" && echo true || echo false`
\if :app_ro_set
ALTER ROLE app_ro PASSWORD :'app_ro_scram';
\endif
\getenv migrator_scram MIGRATOR_SCRAM
\set migrator_set `test -n "${MIGRATOR_SCRAM-}" && echo true || echo false`
\if :migrator_set
ALTER ROLE migrator PASSWORD :'migrator_scram';
\endif
