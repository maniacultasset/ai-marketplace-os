-- 0038/01_columns.down.sql (local and CI only): drop the setting (its grant goes with it).
ALTER TABLE public.tenants DROP COLUMN auto_fix_enabled;
