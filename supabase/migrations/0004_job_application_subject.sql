-- ============================================================================
-- 0004_job_application_subject.sql — per-job application subject rule
--
-- Each job declares the exact phrase the candidate must put at the start of
-- the email subject (e.g. "AI Engineer Application"). Inbox sync targets
-- Gmail with `subject:"<phrase>"` and then verifies the real prefix, linking
-- the application to the job. NULL means "no rule" (generic inbox sync).
-- ============================================================================

alter table public.jobs
    add column if not exists application_subject_prefix text;

create index if not exists ix_jobs_ws_subject_rule
    on public.jobs(workspace_id)
    where application_subject_prefix is not null;