-- 0027/06_imports_column_map_id.up.sql (SI14, AC8): the confirmed map an upload used (a soft
-- reference for the audit and the import job; the map has no DELETE grant, so it outlives the
-- import except for the purge).
-- (The read-only roles hold table-level SELECT on imports, so no column grant is needed.)
ALTER TABLE public.imports ADD COLUMN column_map_id uuid;
