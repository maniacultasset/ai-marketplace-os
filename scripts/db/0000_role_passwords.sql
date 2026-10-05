-- scripts/db/0000_role_passwords.sql: psql only (\getenv, \if, backquotes); never run by psycopg.
-- Sets role passwords from SCRAM-SHA-256 verifiers made by scripts/db/scram_verifier.py and held in
-- the operator's environment as APP_RW_SCRAM, APP_RO_SCRAM and MIGRATOR_SCRAM; unset or empty skips
-- that role. The shell checks every value first, reading the environment (never argv), against the
-- helper's format (iterations 1-9 digits, no leading zero; a non-empty base64 salt; StoredKey and
-- ServerKey of 44 base64 characters ending in one '='), since PostgreSQL hashes any value it cannot
-- parse as a verifier as a plaintext password; one bad value stops the run before any value is
-- sent. Then the server must not log statements or keep their text (pg_stat_statements with utility
-- tracking: set the Cloud SQL flag pg_stat_statements.track_utility=off first), and every target
-- role must exist and be alterable. An ALTER ROLE can still fail (lock or statement timeout, cancel,
-- read-only session, replica), and log_min_error_statement would then log it with its verifier, so
-- it must be panic: the file sets it when the login may (superuser, or GRANT SET), otherwise set the
-- Cloud SQL flag log_min_error_statement=panic first. A deadlock report logs every waiting
-- statement whatever log_min_error_statement says, so after BEGIN the file first takes each target
-- role's pg_authid row with a no-op ALTER ROLE ... CONNECTION LIMIT (its current value); the
-- PASSWORD ALTERs then never wait on a lock, so none appears in a deadlock report.
-- The ALTER ROLEs run in one transaction, so a failure sets no password, and no ALTER ROLE carrying
-- a verifier is logged or kept. Each one is visible in pg_stat_activity.query (superusers,
-- pg_read_all_stats) until the next statement runs, milliseconds later.
\set ON_ERROR_STOP on
\set VERBOSITY terse
\set scram_ok `for v in "${APP_RW_SCRAM-}" "${APP_RO_SCRAM-}" "${MIGRATOR_SCRAM-}"; do case "$v" in '') continue ;; *[!A-Za-z0-9+/=:\$-]*) echo false; exit 0 ;; 'SCRAM-SHA-256$'[1-9]*:*'$'*:*) ;; *) echo false; exit 0 ;; esac; r=${v#'SCRAM-SHA-256$'}; i=${r%%:*}; r=${r#*:}; s=${r%%'$'*}; r=${r#*'$'}; k=${r%%:*}; e=${r#*:}; case "$i" in *[!0-9]*|??????????*) echo false; exit 0 ;; esac; case "$s" in ''|*[!A-Za-z0-9+/=]*|*=*[!=]*|*===) echo false; exit 0 ;; esac; [ $((${#s} % 4)) -eq 0 ] && [ "$e" != "$r" ] || { echo false; exit 0; }; for x in "$k" "$e"; do case "${x%=}" in *[!A-Za-z0-9+/]*) echo false; exit 0 ;; esac; [ ${#x} -eq 44 ] && [ "${x%=}" != "$x" ] || { echo false; exit 0; }; done; done; echo true`
\if :scram_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'a *_SCRAM value is not a SCRAM-SHA-256 verifier; no password was set'; END $$;
\endif
\set any_set `for v in "${APP_RW_SCRAM-}" "${APP_RO_SCRAM-}" "${MIGRATOR_SCRAM-}"; do test -n "$v" && { echo true; exit 0; }; done; echo false`
\if :any_set
SELECT current_setting('log_min_error_statement') <> 'panic'
   AND has_parameter_privilege('log_min_error_statement', 'SET') AS lmes_settable \gset
\if :lmes_settable
SET log_min_error_statement = panic;
\endif
SELECT current_setting('log_statement') = 'none'
   AND current_setting('log_min_error_statement') = 'panic'
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
DO $$ BEGIN RAISE EXCEPTION 'the server logs or keeps statements or failing statements, or a target role is missing or not alterable; no password was set'; END $$;
\endif
BEGIN;
SELECT max(rolconnlimit) FILTER (WHERE rolname = 'app_rw') AS app_rw_limit,
       max(rolconnlimit) FILTER (WHERE rolname = 'app_ro') AS app_ro_limit,
       max(rolconnlimit) FILTER (WHERE rolname = 'migrator') AS migrator_limit
  FROM pg_roles WHERE rolname IN ('app_rw', 'app_ro', 'migrator') \gset
\endif
\set app_rw_set `test -n "${APP_RW_SCRAM-}" && echo true || echo false`
\set app_ro_set `test -n "${APP_RO_SCRAM-}" && echo true || echo false`
\set migrator_set `test -n "${MIGRATOR_SCRAM-}" && echo true || echo false`
\if :app_rw_set
ALTER ROLE app_rw CONNECTION LIMIT :app_rw_limit;
\endif
\if :app_ro_set
ALTER ROLE app_ro CONNECTION LIMIT :app_ro_limit;
\endif
\if :migrator_set
ALTER ROLE migrator CONNECTION LIMIT :migrator_limit;
\endif
\getenv app_rw_scram APP_RW_SCRAM
\if :app_rw_set
ALTER ROLE app_rw PASSWORD :'app_rw_scram';
\endif
\getenv app_ro_scram APP_RO_SCRAM
\if :app_ro_set
ALTER ROLE app_ro PASSWORD :'app_ro_scram';
\endif
\getenv migrator_scram MIGRATOR_SCRAM
\if :migrator_set
ALTER ROLE migrator PASSWORD :'migrator_scram';
\endif
\if :any_set
COMMIT;
\endif
