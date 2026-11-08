-- 0017/01_reset_links.up.sql
-- SI5 (ADR-0029): the copied password reset links. A tenant table under the 0005 template plus a
-- SELECT branch on the token hash, so the public redeem step finds its row before it knows the
-- tenant (the `invitations_hash` pattern). The raw token exists only in the copied link; only its
-- sha256 is stored, and the read-only roles never read it. A link lives at most 24 hours, is used
-- or revoked once, and a member has at most one live link.
CREATE TABLE public.password_reset_links (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  token_hash bytea NOT NULL,
  issued_by_user_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_password_reset_links PRIMARY KEY (id),
  CONSTRAINT uq_password_reset_links_tenant_id_id UNIQUE (tenant_id, id),
  CONSTRAINT uq_password_reset_links_token_hash UNIQUE (token_hash),
  CONSTRAINT fk_password_reset_links_tenant_id_tenants FOREIGN KEY (tenant_id)
    REFERENCES public.tenants (id) ON DELETE CASCADE,
  CONSTRAINT fk_password_reset_links_user_id FOREIGN KEY (tenant_id, user_id)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT fk_password_reset_links_issued_by_user_id FOREIGN KEY (tenant_id, issued_by_user_id)
    REFERENCES public.tenant_memberships (tenant_id, user_id),
  CONSTRAINT ck_password_reset_links_token_hash CHECK (octet_length(token_hash) = 32),
  CONSTRAINT ck_password_reset_links_used_revoked CHECK (used_at IS NULL OR revoked_at IS NULL),
  CONSTRAINT ck_password_reset_links_ttl CHECK (expires_at <= created_at + interval '24 hours')
);

CREATE UNIQUE INDEX uq_password_reset_links_live ON public.password_reset_links (tenant_id, user_id)
  WHERE used_at IS NULL AND revoked_at IS NULL;

ALTER TABLE public.password_reset_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.password_reset_links FORCE ROW LEVEL SECURITY;
CREATE POLICY t_tenant ON public.password_reset_links TO app_rw
  USING (tenant_id = (SELECT public.app_tenant_id()))
  WITH CHECK (tenant_id = (SELECT public.app_tenant_id()));
CREATE POLICY t_platform ON public.password_reset_links FOR SELECT TO app_platform_ro, app_ro
  USING ((current_user IN ('app_platform_ro', 'app_ro')));
CREATE POLICY t_migrator ON public.password_reset_links TO migrator USING (true) WITH CHECK (true);
CREATE POLICY password_reset_links_hash ON public.password_reset_links FOR SELECT TO app_rw
  USING (token_hash = (SELECT public.app_token_hash()));

-- app_rw (the 0000 default privileges grant SELECT, INSERT, UPDATE, DELETE): no TRUNCATE, UPDATE
-- only to close a link; DELETE stays for the retention prune, scoped by t_tenant (CP-D17).
REVOKE TRUNCATE, UPDATE ON public.password_reset_links FROM app_rw;
GRANT UPDATE (used_at, revoked_at) ON public.password_reset_links TO app_rw;
GRANT SELECT (id, tenant_id, user_id, issued_by_user_id, expires_at, used_at, revoked_at, created_at)
  ON public.password_reset_links TO app_ro, app_platform_ro;
