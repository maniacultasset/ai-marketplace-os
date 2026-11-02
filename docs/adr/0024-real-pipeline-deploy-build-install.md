# ADR-0024: The AI team's pipeline enters the deploy image only, by commit sha and a read-only deploy key; the generation route is configurable

- **Status:** proposed (owner decisions in chat, 2026-11-02; closes T104)
- **Date:** 2026-11-02
- **Deciders:** Tech Lead (the user)
- **Amends:** ADR-0012 §3 ("installs it at a pinned git tag, recorded in `uv.lock`") and its
  Consequences line on CI read access; ADR-0013 §5 ("reached through WIF", "settled in S4/S14").

## Context

- The AI team ships `mkos-pipeline` from its own private repository,
  `maniacultasset/mkos-pipeline`. Its only runtime dependencies are `mkos-schemas`, `httpx` and
  `pydantic`, and its import name is `mkos_pipeline`, the same as our in-repo stub
  (`packages/pipeline-stub`, distribution `mkos-pipeline-stub`). The two cannot share one venv.
- ADR-0012 §3 said the worker installs the pipeline at a git tag recorded in `uv.lock`, so CI
  would need read access to the private repository. ADR-0013 §5 later narrowed that: PR and
  nightly jobs never fetch the pipeline; only the deploy build does, with a read-only credential
  in GCP Secret Manager. It left the mechanism open (T104). `tests/meta/test_workspace.py` holds
  the lock to the stub.
- A production worker refuses the stub at boot (`app/core/config.py`), so prod runs no worker
  (`prod.env` `WORKER_ENABLED=false`). Cloud Build had no way to reach the private repository.
- The pipeline pins `mkos-schemas` 2.0.0 by git commit; the backend is on 2.1.0, which differs
  only in `SCHEMA_VERSION`. Installing the pipeline with its own resolution would fight the
  workspace member.
- The model and provider are the AI team's choice. They use OpenRouter, which is
  OpenAI-compatible. The seam's `ModelCredentials.api_base` already carries a validated https base
  (`mkos_schemas/jobs.py`), but the backend always sent `None`.
- Deploys are run by hand with `scripts/deploy/gcp/deploy.sh` (Cloud Build under the `aimos-build`
  service account), not from GitHub, so there is no WIF identity in the path.

## Decision

1. **Stub everywhere but the deploy image.** CI, local runs, the tests and `uv.lock` keep
   `mkos-pipeline-stub`. No GitHub secret and no CI job ever reads the private repository.
2. **One build arg.** `apps/api/Dockerfile` takes `PIPELINE_REF`. Empty (the default) builds the
   stub image as before: BuildKit skips the new stages, and the runtime stage copies the same
   builder. Set, it must be a full 40-character commit sha of `mkos-pipeline` (a tag is not
   accepted: a sha cannot move). The env file (`PIPELINE_REF=` in `prod.env`, `staging.env`) is the
   one place it is set, so a new ref is a new commit and a new `sha-<12>` image tag.
3. **Fetch.** A throwaway stage installs `git` and `openssh-client`, mounts the deploy key as the
   BuildKit secret `mkos_pipeline_key` for one `RUN`, fetches exactly that commit over SSH
   (`git@github.com:maniacultasset/mkos-pipeline.git`), checks `HEAD` equals the ref and drops
   `.git`. SSH runs with `StrictHostKeyChecking=yes`, `BatchMode=yes`,
   `HostKeyAlgorithms=ssh-ed25519` and a known-hosts line pinned to GitHub's published ed25519 host
   key (`gh api meta`, `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`). Only the source
   tree leaves the stage; the key never lands in a layer, a label or the build log.
4. **Install.** The builder uninstalls `mkos-pipeline-stub`, then installs the source with
   `uv pip install --no-deps`, under the hash-pinned `build-constraints.txt` (hatchling). It keeps
   the workspace `mkos-schemas` 2.1.0 and the locked `httpx` and `pydantic`. The stub's source
   leaves `/src`. The build fails unless `from mkos_pipeline import run_listing_pipeline` works,
   distribution `mkos-pipeline` is installed and `mkos-pipeline-stub` is not.
5. **Credential.** A read-only GitHub deploy key on `maniacultasset/mkos-pipeline` (ed25519, made
   by the owner, never by a script), its private half in the Secret Manager secret
   `MKOS_PIPELINE_DEPLOY_KEY`, with `secretAccessor` for `aimos-build` and no other account.
   `deploy.sh setup` creates the secret and the binding when `PIPELINE_REF` is set, writes no
   value, and prints the owner's command (`ssh-keygen`, `gh repo deploy-key add` without
   `--allow-write`, `gcloud secrets versions add --data-file=`, then delete the files).
   Cloud Build reads it through `availableSecrets` in `scripts/deploy/gcp/cloudbuild-pipeline.yaml`
   and hands it to `docker build --secret` from a 0600 temp file in the step container.
   `cloudbuild.yaml` (the stub build) is unchanged and names no secret. `deploy.sh build` picks the
   config by `PIPELINE_REF`, and refuses before uploading when the secret has no value.
6. **Guard.** `deploy.sh` refuses, before any gcloud call, `WORKER_ENABLED=true` in production with
   an empty `PIPELINE_REF`, and a `PIPELINE_REF` that is not a 40-character lowercase sha. Staging
   keeps its stub worker (`APP_ENV=staging` accepts it). The boot refusal in `config.py` stays.
7. **Generation route.** A new optional setting `LLM_API_BASE` (ENV.md) goes to
   `ModelCredentials.api_base` for `get_model_credentials("generation")` only; vision and embedding
   keep `None`. Settings applies the seam's own rule in every env (https, no userinfo, a host, no
   localhost, private, link-local or loopback address, 2048 characters at most). OpenRouter is
   `LLM_PROVIDER=deepseek`, `LLM_MODEL_DEFAULT=<OpenRouter slug, e.g. deepseek/deepseek-chat>`,
   `LLM_API_BASE=https://openrouter.ai/api/v1`, with the OpenRouter key in `DEEPSEEK_API_KEY`:
   the key name is the provider's, the route is the base. The `mkos-schemas` contract is unchanged.
8. **Worker env and keys.** `deploy.sh` gives the worker `LLM_PROVIDER`, `LLM_MODEL_DEFAULT` and
   `LLM_API_BASE` from the env file, and the api and the worker the daily caps
   `LLM_DAILY_BUDGET_USD` and `LLM_TENANT_DAILY_BUDGET_USD` (the api's generate precheck and the
   worker's claim must agree). `DEEPSEEK_API_KEY` becomes a user secret and a worker secret only
   under `LLM_PROVIDER=deepseek`; `setup` asks for it while `WORKER_ENABLED=true`. A worker outside
   local refuses to boot without the key of `LLM_PROVIDER` (as it already does for the embedding
   and vision keys).

## Consequences

- T104 is answered: the pipeline lives in the AI team's repository and reaches only the deploy
  image, pinned by commit sha. CI stays independent of the AI team's release timing, and the meta
  tests that pin the lock and the workspace to the stub stand.
- The deploy image and CI run different pipelines. The stub-driven tests prove the seam, not the
  real package; the real package is proven by its own tests, the post-install check and a
  real-key run before prod generation is switched on (not part of this ADR).
- `--no-deps` means the backend, not the pipeline's metadata, decides `mkos-schemas`, `httpx` and
  `pydantic`. A pipeline release that needs a new dependency or a newer schemas MAJOR fails the
  import check or the seam's tests, and needs a backend change first.
- The pipeline stage pulls `git` and `openssh-client` from Debian at build time (unpinned
  versions, signed archive); they never reach the runtime image. The fetched code is pinned by
  its commit id.
- The owner holds one more credential: a read-only key to one repository, rotatable by adding a
  new deploy key and secret version, revocable on GitHub at any time. The stub build never needs
  it.
- With an empty `PIPELINE_REF` the image and prod behaviour are as before; prod's worker stays off
  until the owner sets the ref, builds, adds the model keys and sets `WORKER_ENABLED=true`
  (`docs/runbooks/DEPLOY-GCP.md`, "The real pipeline").
- The prod api now carries the env file's daily caps (3 and 1 USD); it carried the Settings
  defaults before. With no worker spending, no request is refused at either cap.
