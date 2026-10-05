-- psql only. Not executed by the Python tests. Skips a role when its env var is unset or empty.
\getenv app_rw_password APP_RW_PASSWORD
\if :{?app_rw_password}
SELECT :'app_rw_password' <> '' AS app_rw_ok \gset
\if :app_rw_ok
ALTER ROLE app_rw PASSWORD :'app_rw_password';
\endif
\endif
\getenv app_ro_password APP_RO_PASSWORD
\if :{?app_ro_password}
SELECT :'app_ro_password' <> '' AS app_ro_ok \gset
\if :app_ro_ok
ALTER ROLE app_ro PASSWORD :'app_ro_password';
\endif
\endif
\getenv migrator_password MIGRATOR_PASSWORD
\if :{?migrator_password}
SELECT :'migrator_password' <> '' AS migrator_ok \gset
\if :migrator_ok
ALTER ROLE migrator PASSWORD :'migrator_password';
\endif
\endif
