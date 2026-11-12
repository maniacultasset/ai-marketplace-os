-- 0038/01_columns.up.sql (client feedback item 20, 11 Nov 2026): the brand setting "Let Kuinn fix
-- safe listing errors automatically", on by default so every brand keeps today's behaviour (a
-- catalog-only change on PostgreSQL 17, no rewrite). app_rw may UPDATE it (the service lets only
-- an active owner do so, after a step-up); the read-only roles already SELECT every tenants
-- column (0001).
ALTER TABLE public.tenants ADD COLUMN auto_fix_enabled boolean NOT NULL DEFAULT true;
GRANT UPDATE (auto_fix_enabled) ON public.tenants TO app_rw;
