-- ============================================================================
-- 0001_schema.sql — AI Recruiter OS core schema
--
-- Mirrors apps/api/app/db.py (SQLAlchemy Core). IDs are 36-char text because
-- the API generates UUID strings (and the scheduler uses uuid4().hex), so no
-- uuid column type is used anywhere.
--
-- Safe to re-run: every object is created with IF NOT EXISTS.
-- ============================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- helpers ---
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- ----------------------------------------------------------------- tenancy --
create table if not exists public.workspaces (
    id          text primary key default gen_random_uuid()::text,
    name        text not null,
    settings    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists public.workspace_members (
    id            text primary key default gen_random_uuid()::text,
    workspace_id  text not null references public.workspaces(id) on delete cascade,
    user_id       text not null,
    email         text,
    display_name  text,
    role          varchar(20) not null default 'owner',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint uq_workspace_member unique (workspace_id, user_id)
);
create index if not exists ix_workspace_members_user on public.workspace_members(user_id);

-- --------------------------------------------------------------- connectors --
create table if not exists public.connected_accounts (
    id                            text primary key default gen_random_uuid()::text,
    workspace_id                  text not null references public.workspaces(id) on delete cascade,
    recruiter_id                  text not null,
    provider                      varchar(40) not null,  -- gmail | microsoft_teams
    composio_connected_account_id text,
    composio_auth_config_id       text,
    composio_user_id              text,
    connected_email               text,
    display_name                  text,
    status                        varchar(20) not null default 'pending',
    scopes                        jsonb not null default '[]'::jsonb,
    last_error                    text,
    last_verified_at              timestamptz,
    created_at                    timestamptz not null default now(),
    updated_at                    timestamptz not null default now(),
    constraint uq_connected_account unique (workspace_id, provider, connected_email)
);
create index if not exists ix_connected_accounts_ws
    on public.connected_accounts(workspace_id, provider);

create table if not exists public.inbox_sync_settings (
    id                     text primary key default gen_random_uuid()::text,
    workspace_id           text not null references public.workspaces(id) on delete cascade,
    connected_account_id   text not null references public.connected_accounts(id) on delete cascade,
    enabled                boolean not null default true,
    interval_minutes       integer not null default 30,
    is_custom_interval     boolean not null default false,
    query                  text not null default 'newer_than:2d',
    last_synced_at         timestamptz,
    next_sync_at           timestamptz,
    last_sync_status       varchar(20),
    last_sync_error        text,
    consecutive_failures   integer not null default 0,
    history_cursor         text,
    overlap_minutes        integer not null default 60,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    constraint uq_sync_settings_account unique (connected_account_id)
);
create index if not exists ix_sync_settings_due
    on public.inbox_sync_settings(enabled, next_sync_at);

create table if not exists public.ingestion_jobs (
    id                    text primary key default gen_random_uuid()::text,
    workspace_id          text not null references public.workspaces(id) on delete cascade,
    connected_account_id  text not null references public.connected_accounts(id) on delete cascade,
    status                varchar(20) not null default 'queued',
    trigger               varchar(20) not null default 'cron',
    attempts              integer not null default 0,
    max_attempts          integer not null default 4,
    locked_by             text,
    locked_at             timestamptz,
    started_at            timestamptz,
    completed_at          timestamptz,
    next_retry_at         timestamptz,
    error_message         text,
    stats                 jsonb not null default '{}'::jsonb,
    created_at            timestamptz not null default now()
);
create index if not exists ix_jobs_ws_created
    on public.ingestion_jobs(workspace_id, created_at);
create index if not exists ix_jobs_status_retry
    on public.ingestion_jobs(status, next_retry_at);

create table if not exists public.email_messages (
    id                    text primary key default gen_random_uuid()::text,
    workspace_id          text not null references public.workspaces(id) on delete cascade,
    connected_account_id  text not null references public.connected_accounts(id) on delete cascade,
    provider_message_id   text not null,
    thread_id             text,
    sender_email          text,
    sender_name           text,
    recipients            jsonb not null default '[]'::jsonb,
    subject               text,
    snippet               text,
    received_at           timestamptz,
    has_attachments       boolean not null default false,
    processing_status     varchar(20) not null default 'pending',
    skip_reason           text,
    error_message         text,
    headers               jsonb not null default '{}'::jsonb,
    created_at            timestamptz not null default now(),
    constraint uq_email_provider_id unique (connected_account_id, provider_message_id)
);
create index if not exists ix_email_messages_ws
    on public.email_messages(workspace_id, received_at);

create table if not exists public.email_attachments (
    id                      text primary key default gen_random_uuid()::text,
    workspace_id            text not null references public.workspaces(id) on delete cascade,
    email_message_id        text not null references public.email_messages(id) on delete cascade,
    provider_attachment_id  text not null,
    filename                text,
    mime_type               text,
    size_bytes              integer not null default 0,
    storage_path            text,
    sha256                  varchar(64),
    status                  varchar(20) not null default 'pending',
    error_message           text,
    created_at              timestamptz not null default now(),
    constraint uq_attachment_provider_id unique (email_message_id, provider_attachment_id)
);
create index if not exists ix_attachments_message
    on public.email_attachments(email_message_id);

-- ------------------------------------------------------------------- hires --
create table if not exists public.candidates (
    id                     text primary key default gen_random_uuid()::text,
    workspace_id           text not null references public.workspaces(id) on delete cascade,
    full_name              text not null,
    email                  text,
    email_normalized       text,
    phone                  text,
    location               text,
    preferred_location     text,
    experience_years       double precision,
    "current_role"         text,
    current_company        text,
    previous_companies     jsonb not null default '[]'::jsonb,
    education              jsonb not null default '[]'::jsonb,
    university             text,
    graduation_year        integer,
    skills                 jsonb not null default '[]'::jsonb,
    programming_languages  jsonb not null default '[]'::jsonb,
    frameworks             jsonb not null default '[]'::jsonb,
    tools                  jsonb not null default '[]'::jsonb,
    projects               jsonb not null default '[]'::jsonb,
    summary                text,
    github_url             text,
    leetcode_url           text,
    linkedin_url           text,
    portfolio_url          text,
    resume_storage_path    text,
    resume_filename        text,
    resume_text            text,
    resume_sha256          varchar(64),
    extraction_method      varchar(20),
    extraction_confidence  double precision,
    extraction             jsonb not null default '{}'::jsonb,
    pipeline_status        varchar(30) not null default 'APPLIED',
    source                 varchar(30) not null default 'gmail',
    owner_id               text,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    constraint uq_candidate_email unique (workspace_id, email_normalized)
);
create index if not exists ix_candidates_ws_status
    on public.candidates(workspace_id, pipeline_status);
create index if not exists ix_candidates_ws_created
    on public.candidates(workspace_id, created_at);

create table if not exists public.jobs (
    id                      text primary key default gen_random_uuid()::text,
    workspace_id            text not null references public.workspaces(id) on delete cascade,
    title                   text not null,
    description             text not null,
    structured_requirements jsonb not null default '{}'::jsonb,
    status                  varchar(20) not null default 'open',
    created_by              text,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);
create index if not exists ix_jobs_ws on public.jobs(workspace_id, status);

create table if not exists public.candidate_applications (
    id                text primary key default gen_random_uuid()::text,
    workspace_id      text not null references public.workspaces(id) on delete cascade,
    candidate_id      text not null references public.candidates(id) on delete cascade,
    email_message_id  text references public.email_messages(id) on delete set null,
    job_id            text references public.jobs(id) on delete set null,
    application_date  timestamptz,
    created_at        timestamptz not null default now(),
    constraint uq_application_message unique (candidate_id, email_message_id)
);
create index if not exists ix_applications_candidate
    on public.candidate_applications(candidate_id);

create table if not exists public.candidate_evaluations (
    id                    text primary key default gen_random_uuid()::text,
    workspace_id          text not null references public.workspaces(id) on delete cascade,
    candidate_id          text not null references public.candidates(id) on delete cascade,
    job_id                text not null references public.jobs(id) on delete cascade,
    match_score           double precision not null default 0,
    skills_score          double precision not null default 0,
    experience_score      double precision not null default 0,
    project_score         double precision not null default 0,
    location_score        double precision not null default 0,
    evidence_score        double precision not null default 0,
    weights               jsonb not null default '{}'::jsonb,
    evaluation_details    jsonb not null default '{}'::jsonb,
    strengths             jsonb not null default '[]'::jsonb,
    missing_requirements  jsonb not null default '[]'::jsonb,
    concerns              jsonb not null default '[]'::jsonb,
    verification_evidence jsonb not null default '{}'::jsonb,
    explanation           text,
    model                 text,
    method                varchar(20) not null default 'heuristic',
    input_hash            varchar(64),
    evaluated_at          timestamptz not null default now(),
    constraint uq_evaluation_candidate_job unique (candidate_id, job_id)
);
create index if not exists ix_evaluations_job_score
    on public.candidate_evaluations(job_id, match_score);

create table if not exists public.candidate_communications (
    id                  text primary key default gen_random_uuid()::text,
    workspace_id        text not null references public.workspaces(id) on delete cascade,
    candidate_id        text not null references public.candidates(id) on delete cascade,
    recruiter_id        text,
    channel             varchar(20) not null default 'email',
    direction           varchar(10) not null default 'outbound',
    kind                varchar(30) not null default 'custom',
    subject             text,
    content             text,
    provider            varchar(30) not null default 'gmail',
    provider_message_id text,
    status              varchar(20) not null default 'draft',
    error_message       text,
    metadata            jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now(),
    sent_at             timestamptz
);
create index if not exists ix_comms_candidate
    on public.candidate_communications(candidate_id, created_at);

create table if not exists public.interviews (
    id                  text primary key default gen_random_uuid()::text,
    workspace_id        text not null references public.workspaces(id) on delete cascade,
    candidate_id        text not null references public.candidates(id) on delete cascade,
    recruiter_id        text,
    job_id              text references public.jobs(id) on delete set null,
    provider            varchar(30) not null default 'microsoft_teams',
    calendar_provider   varchar(30) not null default 'teams',
    calendar_id         text not null default 'primary',
    calendar_event_id   text,
    calendar_html_link  text,
    meeting_id          text,
    meeting_url         text,
    start_time          timestamptz,
    end_time            timestamptz,
    timezone            text not null default 'Asia/Kolkata',
    status              varchar(30) not null default 'scheduled',
    invite_status       varchar(20) not null default 'not_sent',
    attendees           jsonb not null default '[]'::jsonb,
    idempotency_key     text not null,
    notes               text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint uq_interview_idempotency unique (idempotency_key)
);
create index if not exists ix_interviews_ws_start
    on public.interviews(workspace_id, start_time);
create index if not exists ix_interviews_candidate
    on public.interviews(candidate_id);

create table if not exists public.candidate_status_history (
    id            text primary key default gen_random_uuid()::text,
    workspace_id  text not null references public.workspaces(id) on delete cascade,
    candidate_id  text not null references public.candidates(id) on delete cascade,
    from_status   varchar(30),
    to_status     varchar(30) not null,
    changed_by    text,
    reason        text,
    created_at    timestamptz not null default now()
);
create index if not exists ix_status_history_candidate
    on public.candidate_status_history(candidate_id);

create table if not exists public.recruiter_notes (
    id                text primary key default gen_random_uuid()::text,
    workspace_id      text not null references public.workspaces(id) on delete cascade,
    candidate_id      text not null references public.candidates(id) on delete cascade,
    recruiter_id      text,
    note              text not null,
    next_followup_at  timestamptz,
    created_at        timestamptz not null default now()
);
create index if not exists ix_notes_candidate on public.recruiter_notes(candidate_id);

create table if not exists public.recruiter_activity_logs (
    id            text primary key default gen_random_uuid()::text,
    workspace_id  text not null references public.workspaces(id) on delete cascade,
    recruiter_id  text,
    action        text not null,
    entity_type   text,
    entity_id     text,
    metadata      jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index if not exists ix_activity_ws_created
    on public.recruiter_activity_logs(workspace_id, created_at);

create table if not exists public.pending_actions (
    id             text primary key default gen_random_uuid()::text,
    workspace_id   text not null references public.workspaces(id) on delete cascade,
    recruiter_id   text,
    kind           varchar(40) not null,
    title          text not null,
    payload        jsonb not null default '{}'::jsonb,
    status         varchar(20) not null default 'pending',
    result         jsonb not null default '{}'::jsonb,
    error_message  text,
    created_at     timestamptz not null default now(),
    expires_at     timestamptz,
    executed_at    timestamptz
);
create index if not exists ix_pending_actions_ws
    on public.pending_actions(workspace_id, status);

create table if not exists public.chat_threads (
    id            text primary key default gen_random_uuid()::text,
    workspace_id  text not null references public.workspaces(id) on delete cascade,
    user_id       text,
    title         text not null default 'New conversation',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists ix_chat_threads_ws
    on public.chat_threads(workspace_id, updated_at);

create table if not exists public.chat_messages (
    id            text primary key default gen_random_uuid()::text,
    workspace_id  text not null references public.workspaces(id) on delete cascade,
    thread_id     text not null references public.chat_threads(id) on delete cascade,
    role          varchar(20) not null,
    content       text not null,
    payload       jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index if not exists ix_chat_messages_thread
    on public.chat_messages(thread_id, created_at);

-- ------------------------------------------------------- updated_at triggers --
do $$
declare
    target text;
begin
    foreach target in array array[
        'workspaces', 'workspace_members', 'connected_accounts',
        'inbox_sync_settings', 'candidates', 'jobs', 'interviews',
        'chat_threads'
    ] loop
        execute format('drop trigger if exists trg_%s_updated_at on public.%I', target, target);
        execute format(
            'create trigger trg_%s_updated_at before update on public.%I '
            'for each row execute function public.set_updated_at()',
            target, target
        );
    end loop;
end;
$$;

-- ------------------------------------------------------------ storage bucket --
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'resumes',
    'resumes',
    false,
    10485760,
    array[
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain'
    ]
)
on conflict (id) do update
set file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
