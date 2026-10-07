# ADR-0018: Marketplace token envelope encryption

- **Status:** proposed
- **Date:** 2026-10-07
- **Deciders:** Tech Lead (the user); proposed by the agent building S15

## Context

Invariant 6 says marketplace credentials never enter a prompt and are KMS-encrypted at rest. The
spec asks for access and refresh tokens "encrypted with AES-256" (L5262-5263) and for decryption
"in-memory only when the adapter needs to make an API call" (L12512, L12526).
BACKEND-ARCHITECTURE.md §9.4 asks for a fresh AES-256-GCM key per write, bound to the brand as
AAD, "pinned in a crypto ADR" (open questions OQ6 and OQ20). EXECUTION-PLAN decision 15 splits the
roles: the API encrypts only, and anything that needs plaintext runs as a worker job. Locally and
in CI there is no Cloud KMS and agents hold no cloud credentials (CANON-ADDENDUM A§9:
`LocalKmsStub`, one 32-byte dev key, so a laptop and CI open each other's blobs).

The OAuth callback is public (no JWT), so it cannot create the account itself (S3 CP4): it must
park the exchanged tokens until the user who started the flow confirms them.

## Decision

Tokens are sealed with envelope encryption, and the seal and unseal halves live in different
layers.

- **Seal** (`app/core/crypto.py`; the API and the worker may import it): a fresh 32-byte data key
  and a fresh 96-bit nonce per seal; AES-256-GCM over the compact JSON `{"v":1,"a":…,"r":…}` with
  the tenant id (16 bytes) as AAD, so a blob copied to another tenant's row never opens. The data
  key is wrapped by a `KeyWrapper`; the blob is `MKT1` | u16 wrapped length | wrapped key | nonce
  | ciphertext and tag, stored in `token_enc` with the wrapper's key version in
  `kms_key_version`.
- **Wrapper** chosen at first use, never at import (CANON.md:163): `APP_ENV=local` uses
  `LocalKmsStub` (AES-256-GCM under `KMS_LOCAL_DEV_KEY`, AAD `mkos-dek|<version>|<tenant>`, version
  `local-1`); every other environment uses Cloud KMS
  `projects/<project>/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key`
  (CANON.md:70), which lands with S4. Until then a deployed api or worker refuses to seal or unseal
  (`NotImplementedError`), and the boot refuses a missing or forbidden `KMS_KEY_URI` and any
  `KMS_LOCAL_DEV_KEY`.
- **Unseal** (`app/adapters/credentials.py`): only `app.adapters.*` and `app.workers.*` import it,
  directly or indirectly (import contract `decrypt-path-only-in-adapters-and-workers`). Every
  failure is one detail-free `CredentialUnsealError`. Plaintext lives only in the caller's locals as
  `SecretStr`.
- **Pending store:** the callback seals the exchanged tokens into an `oauth_states` pending row
  (revision 0006) under the sha256 of a fresh opaque handle; the authenticated confirm moves the
  same blob to `marketplace_accounts`. The public hash branch never sees a pending row.
- **Refresh** runs in the worker (`app/workers/token_refresh.py`): unseal, refresh, re-seal, with
  no transaction open across the marketplace call, and a `credential_used` audit row each time the
  credential is unsealed.
- **Revoke** runs in the API with no plaintext: it sets `token_enc` and `kms_key_version` NULL.

## Consequences

- A leaked database row or backup alone yields no token: the data key is wrapped by a key the
  database never holds, and the AAD binds each blob to its tenant.
- Revoke deletes the live copy of the tokens; it is **not a crypto-shred**. The wrapped blob stays
  in backups for the 7-day PITR window (ARCH.md:114) under a key-encryption key that is still live,
  and the refresh token stays valid at the marketplace until the brand removes the app in Seller
  Central or the Flipkart seller dashboard (D9). The revoke's API description says so.
- No service or router can reach plaintext, so a connection-health check or a revoke that wants
  to call the marketplace must be a worker job.
- Rotating the key-encryption key needs a re-wrap pass (a worker job) keyed by `kms_key_version`;
  not built in v1.
- S4 must add the Cloud KMS wrapper and unwrapper behind the same two protocols, with the
  decision-15 grants (API: encrypt; worker: encrypt and decrypt).
