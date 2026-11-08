-- 0022/03_catalog.up.sql
-- SI11: the growing error database's read side. One row per marketplace code seen on an adapter
-- issue, across tenants, for staff (the ops page and `app.cli errors catalog`): no tenant id, no
-- message text, only counts. security_invoker, so the caller's RLS and grants apply; only
-- app_platform_ro may read it (DATABASE.md li_unknown_codes "read cross-tenant only via
-- app_platform_ro, to grow the error map").
CREATE VIEW public.marketplace_error_catalog WITH (security_invoker = true) AS
SELECT g.marketplace, g.marketplace_code, g.error_code, g.error_class, g.mapped, g.issues,
       g.occurrences, g.tenants, g.first_seen, g.last_seen, g.preview,
       (SELECT jsonb_object_agg(r.resolution, r.n)
          FROM (SELECT coalesce(i.resolution, 'open') AS resolution, count(*) AS n
                  FROM public.listing_issues i
                 WHERE i.source IN ('amazon_adapter', 'flipkart_adapter')
                   AND i.marketplace = g.marketplace
                   AND i.marketplace_code IS NOT DISTINCT FROM g.marketplace_code
                   AND i.error_code = g.error_code AND i.error_class = g.error_class
                 GROUP BY 1) r) AS resolutions,
       g.recurrence_rate
  FROM (SELECT li.marketplace, li.marketplace_code, li.error_code, li.error_class,
               bool_or(li.error_map_id IS NOT NULL) AS mapped,
               count(*) AS issues,
               sum(li.occurrences)::bigint AS occurrences,
               count(DISTINCT li.tenant_id) AS tenants,
               min(li.first_seen_at) AS first_seen,
               max(li.last_seen_at) AS last_seen,
               bool_or(li.dedupe_key LIKE '%|preview') AS preview,
               round(count(li.recurrence_of_id)::numeric / count(*), 3) AS recurrence_rate
          FROM public.listing_issues li
         WHERE li.source IN ('amazon_adapter', 'flipkart_adapter')
         GROUP BY li.marketplace, li.marketplace_code, li.error_code, li.error_class) g;

REVOKE ALL ON public.marketplace_error_catalog FROM PUBLIC, app_rw, app_ro;
GRANT SELECT ON public.marketplace_error_catalog TO app_platform_ro;
