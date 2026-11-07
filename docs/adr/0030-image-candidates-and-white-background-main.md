# ADR-0030: Image candidates and the AI white-background main

- **Status:** proposed (SI7 image generation, 2026-11-07)
- **Date:** 2026-11-07
- **Deciders:** Tech Lead (user, chat 2026-11-07, V1 delegation); AI and FE leads unnamed (P8)

## Context

The user put AI image generation into V1 on 2026-11-07, reversing the earlier V2 call. The spec defers "advanced
image generative editing" (L4763) and says generation must never be a V1 listing dependency (L2249-2251).

Sellers' main photos often fail Amazon's main-image rules: pure white RGB 255,255,255, a square frame, at least
1000 px and a fill of about 85 % (L5173, L10034, L10079). Today the only path is a re-upload (T85).

The client is bootstrapped. The OpenRouter key has a USD 10 total credit limit, with daily caps of USD 3 for the
platform and USD 1 per brand. The backend never imports a model SDK, and the AI seam had one entry point (CANON.md
"The AI seam").

## Decision

1. Three ways to get a better image. Each produces a **candidate**, never an image:
   - (a) `white_background`: one Gemini Flash Image edit through OpenRouter (`LLM_MODEL_IMAGE_EDIT`, default
     `google/gemini-2.5-flash-image`, `data_collection: deny`). The backend then composites the seller's own pixels
     back over the model's background.
   - (b) `fix`: deterministic Pillow framing (whiten a near-white border, trim, square, 85 % fill, 1000-2000 px).
     No global colour change.
   - (c) `size_chart`: a deterministic Pillow render of the seller's size chart and fabric.
   Lifestyle and model shots are out of V1.
2. Candidates live in a new tenant table `image_candidates`. A candidate becomes an `images` row only when the seller
   accepts it, with provenance `origin` (`seller|generated|rendered`), `parent_image_id` and `candidate_id`. The
   old main is kept as a secondary image. No listing state waits on a candidate. An accepted image reaches a
   marketplace only in a new listing version that another user approves (invariants 1 and 3).
3. A deterministic fidelity check (mask IoU, pixel agreement, colour histogram distance and the share of product
   colours turned white) discards a model output that moved, scaled, recoloured or erased part of the product. The image compliance checks then judge the result like any upload.
4. The AI seam gains a second entry point: `edit_image(ImageEditRequest, ModelCredentials) -> ImageEditResult` in
   `mkos-pipeline` (0.4.0). It is called only by `app/workers/image_edit_seam.py`, through a lazy lookup, so an older
   pipeline pin degrades to "not available".
5. Cost control:
   - one model call per (product, source `clean_content_hash`, model and prompt version), cached;
   - `max_attempts=1` and no automatic retry;
   - USD 0.05 reserved per call (about USD 0.039 real);
   - one `agent_runs` row per call (invariant 7);
   - the feature is off by default (`IMAGE_EDIT_ENABLED`) and turned on in prod only after one live call by the user
     confirms the deny route, the response shape and the token count.
6. No closed vocabulary grows. The candidate job reuses the `image_compliance` kind with a `candidate_id` payload
   and the DB-only entity type `image_candidate`, so the contract change is MINOR (RULES.md §11).

## Consequences

- Sellers can fix the most common main-image failure in one tap, at zero tokens when Pillow suffices. That closes
  R10079 and R10034 and reverses R04763 (R04763a stays deferred).
- A generated image is always reviewable against its original, and the original is never lost.
- Adding `lifestyle` to `EditTask` later is MAJOR and waits for a contract window.
- The pipeline repo owns the prompt, the price row and the provider parsing. The Chat Completions image field is
  confirmed only by the live call.
- Rejected and failed candidates are deleted after 30 days. Accepted ones stay as provenance. Candidate objects of a
  purged tenant are removed with its prefix (KNOWN-ISSUES).
- Gemini Flash Image output is about 1024 px. Compositing keeps the seller's resolution for the product, and
  `google/gemini-3.1-flash-image` (2K, about twice the price) is a setting change after an eval.
