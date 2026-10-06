-- 0004/01_transitions.up.sql
-- DATABASE.md §6.1 "listing_status_transitions": platform-global, no RLS, SELECT-only for the app
-- roles, changed only by migration. 9 statuses, the 30-value `last_event` vocabulary (23 events
-- with a status change, DATABASE.md:1376-1399, plus the 7 without one, :1400), 63 seeded edges.
-- The same 30 values become `ck_listings_last_event` (S7 left `listings.last_event` unchecked).
CREATE TABLE public.listing_status_transitions (
  from_status text NOT NULL,
  to_status text NOT NULL,
  event text NOT NULL,
  actor text NOT NULL,
  requires_current_approval boolean NOT NULL DEFAULT false,
  voids_approval boolean NOT NULL DEFAULT false,
  allowed_reasons text[],
  requires_from_reason text[],
  CONSTRAINT pk_listing_status_transitions PRIMARY KEY (from_status, to_status, event),
  CONSTRAINT ck_listing_status_transitions_from_status CHECK (from_status IN (
    'created', 'generating', 'validated', 'needs_review', 'approved', 'publishing', 'published',
    'blocked', 'failed')),
  CONSTRAINT ck_listing_status_transitions_to_status CHECK (to_status IN (
    'created', 'generating', 'validated', 'needs_review', 'approved', 'publishing', 'published',
    'blocked', 'failed')),
  CONSTRAINT ck_listing_status_transitions_event CHECK (event IN (
    'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
    'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
    'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
    'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
    'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
    'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive')),
  CONSTRAINT ck_listing_status_transitions_actor CHECK (actor IN ('user', 'worker', 'any')),
  -- to ≠ from, except the one reason-only row (blocked → blocked on human_task_opened)
  CONSTRAINT ck_listing_status_transitions_moves CHECK (
    to_status <> from_status
    OR (from_status = 'blocked' AND event = 'human_task_opened')),
  -- allowed_reasons set ⇔ the target is blocked or failed
  CONSTRAINT ck_listing_status_transitions_reasons CHECK (
    (allowed_reasons IS NOT NULL) = (to_status IN ('blocked', 'failed')))
);

INSERT INTO public.listing_status_transitions
  (from_status, to_status, event, actor, requires_current_approval, voids_approval,
   allowed_reasons, requires_from_reason)
VALUES
  ('created', 'generating', 'generate', 'user', false, false, NULL, NULL),
  ('generating', 'validated', 'generation_clean', 'worker', false, false, NULL, NULL),
  ('generating', 'needs_review', 'generation_warnings', 'worker', false, false, NULL, NULL),
  ('generating', 'blocked', 'corrections_exhausted', 'worker', false, false, '{corrections_exhausted}', NULL),
  ('generating', 'blocked', 'human_task_opened', 'worker', false, false, '{human_task_open}', NULL),
  ('generating', 'failed', 'generation_failed', 'worker', false, false, '{generation_failed}', NULL),
  ('validated', 'blocked', 'human_task_opened', 'any', false, false, '{human_task_open}', NULL),
  ('needs_review', 'blocked', 'human_task_opened', 'any', false, false, '{human_task_open}', NULL),
  ('approved', 'blocked', 'human_task_opened', 'any', false, true, '{human_task_open}', NULL),
  ('failed', 'blocked', 'human_task_opened', 'any', false, false, '{human_task_open}', NULL),
  ('published', 'blocked', 'human_task_opened', 'any', false, false, '{human_task_open}', NULL),
  ('blocked', 'blocked', 'human_task_opened', 'any', false, false, '{human_task_open}', '{validation_errors}'),
  ('validated', 'approved', 'approve', 'user', false, false, NULL, NULL),
  ('needs_review', 'approved', 'approve', 'user', false, false, NULL, NULL),
  ('validated', 'blocked', 'reject', 'user', false, false, '{rejected}', NULL),
  ('needs_review', 'blocked', 'reject', 'user', false, false, '{rejected}', NULL),
  ('validated', 'generating', 'reject_regenerate', 'user', false, true, NULL, NULL),
  ('needs_review', 'generating', 'reject_regenerate', 'user', false, true, NULL, NULL),
  ('validated', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('needs_review', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('approved', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('blocked', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('failed', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('published', 'generating', 'regenerate', 'user', false, true, NULL, NULL),
  ('validated', 'needs_review', 'edit', 'user', false, true, NULL, NULL),
  ('validated', 'blocked', 'edit', 'user', false, true, '{validation_errors}', NULL),
  ('needs_review', 'validated', 'edit', 'user', false, true, NULL, NULL),
  ('needs_review', 'blocked', 'edit', 'user', false, true, '{validation_errors}', NULL),
  ('approved', 'validated', 'edit', 'user', false, true, NULL, NULL),
  ('approved', 'needs_review', 'edit', 'user', false, true, NULL, NULL),
  ('approved', 'blocked', 'edit', 'user', false, true, '{validation_errors}', NULL),
  ('blocked', 'validated', 'edit', 'user', false, true, NULL, NULL),
  ('blocked', 'needs_review', 'edit', 'user', false, true, NULL, NULL),
  ('failed', 'validated', 'edit', 'user', false, true, NULL, NULL),
  ('failed', 'needs_review', 'edit', 'user', false, true, NULL, NULL),
  ('failed', 'blocked', 'edit', 'user', false, true, '{validation_errors}', NULL),
  ('published', 'validated', 'edit', 'user', false, true, NULL, NULL),
  ('published', 'needs_review', 'edit', 'user', false, true, NULL, NULL),
  ('published', 'blocked', 'edit', 'user', false, true, '{validation_errors}', NULL),
  ('validated', 'needs_review', 'revalidate', 'any', false, false, NULL, NULL),
  ('validated', 'blocked', 'revalidate', 'any', false, false, '{validation_errors}', NULL),
  ('needs_review', 'validated', 'revalidate', 'any', false, false, NULL, NULL),
  ('needs_review', 'blocked', 'revalidate', 'any', false, false, '{validation_errors}', NULL),
  ('approved', 'needs_review', 'revalidate', 'any', false, true, NULL, NULL),
  ('approved', 'blocked', 'revalidate', 'any', false, true, '{validation_errors}', NULL),
  ('blocked', 'validated', 'revalidate', 'any', false, false, NULL, NULL),
  ('blocked', 'needs_review', 'revalidate', 'any', false, false, NULL, NULL),
  ('publishing', 'needs_review', 'revalidate', 'any', false, true, NULL, NULL),
  ('publishing', 'blocked', 'revalidate', 'any', false, true, '{validation_errors}', NULL),
  ('blocked', 'generating', 'task_answered', 'user', false, false, NULL, '{human_task_open}'),
  ('blocked', 'validated', 'task_answered', 'user', false, false, NULL, '{human_task_open}'),
  ('blocked', 'needs_review', 'task_answered', 'user', false, false, NULL, '{human_task_open}'),
  ('approved', 'publishing', 'publish', 'user', true, false, NULL, NULL),
  ('publishing', 'published', 'submission_accepted', 'worker', false, false, NULL, NULL),
  ('publishing', 'failed', 'submission_failed', 'any', false, false, '{publish_failed,publish_partial}', NULL),
  ('publishing', 'blocked', 'submission_attempts_exhausted', 'worker', false, false, '{publish_attempts_exhausted}', NULL),
  ('publishing', 'blocked', 'marketplace_action_required', 'worker', false, false, '{marketplace_action_required}', NULL),
  ('published', 'failed', 'marketplace_rejected', 'worker', false, false, '{marketplace_rejected}', NULL),
  ('published', 'blocked', 'marketplace_action_required', 'worker', false, false, '{marketplace_action_required}', NULL),
  ('published', 'blocked', 'sync_exhausted', 'worker', false, false, '{marketplace_action_required}', NULL),
  ('failed', 'publishing', 'retry_publish', 'user', true, false, NULL, '{publish_failed,publish_partial,marketplace_rejected}'),
  ('failed', 'publishing', 'auto_resubmit', 'worker', true, false, NULL, NULL),
  ('blocked', 'publishing', 'resume_publish', 'user', true, false, NULL, '{marketplace_action_required}');

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.listing_status_transitions FROM app_rw;
GRANT SELECT ON public.listing_status_transitions TO app_rw, app_ro, app_platform_ro;

-- The table is empty in every environment (no data yet), so a plain ADD is instant.
ALTER TABLE public.listings ADD CONSTRAINT ck_listings_last_event CHECK (last_event IN (
  'generate', 'generation_clean', 'generation_warnings', 'corrections_exhausted',
  'human_task_opened', 'generation_failed', 'approve', 'reject', 'reject_regenerate',
  'regenerate', 'edit', 'revalidate', 'task_answered', 'publish', 'submission_accepted',
  'submission_failed', 'submission_attempts_exhausted', 'marketplace_action_required',
  'marketplace_rejected', 'sync_exhausted', 'retry_publish', 'auto_resubmit', 'resume_publish',
  'create', 'sync', 'refresh_requested', 'pause', 'unpause', 'archive', 'unarchive'));
