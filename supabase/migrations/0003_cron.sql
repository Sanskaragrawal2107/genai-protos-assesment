-- ============================================================================
-- 0003_cron.sql — scheduled ingestion + scheduler heartbeat
--
-- Requires the pg_cron and pg_net extensions (available on Supabase).
--
-- Before this job can reach the Edge Functions, add two Vault secrets
-- (Supabase dashboard → Project Settings → Vault, or `vault.create_secret`):
--
--   supabase_project_url       e.g. https://<project-ref>.supabase.co
--   supabase_service_role_key  the service-role key (server secret)
--
-- Until both exist the jobs are still scheduled, but they log a notice and
-- perform no network call. Nothing is faked.
-- ============================================================================

create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.invoke_edge_function(
    fn_name text,
    body jsonb default '{}'::jsonb
)
returns bigint
language plpgsql
security definer
set search_path = public, net, vault
as $$
declare
    project_url text;
    service_key text;
    request_id  bigint;
begin
    select decrypted_secret into project_url
      from vault.decrypted_secrets
     where name = 'supabase_project_url'
     limit 1;

    select decrypted_secret into service_key
      from vault.decrypted_secrets
     where name = 'supabase_service_role_key'
     limit 1;

    if project_url is null or service_key is null then
        raise notice
            'invoke_edge_function(%): missing Vault secrets '
            'supabase_project_url / supabase_service_role_key',
            fn_name;
        return null;
    end if;

    select net.http_post(
        url := rtrim(project_url, '/') || '/functions/v1/' || fn_name,
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'Authorization', 'Bearer ' || service_key
        ),
        body := body,
        timeout_milliseconds := 25000
    )
    into request_id;

    return request_id;
end;
$$;

revoke all on function public.invoke_edge_function(text, jsonb) from public;

-- ------------------------------------------------------------------ schedule --
do $$
begin
    if exists (select 1 from cron.job where jobname = 'recruiter-scheduler') then
        perform cron.unschedule('recruiter-scheduler');
    end if;
    if exists (select 1 from cron.job where jobname = 'recruiter-ingest-worker') then
        perform cron.unschedule('recruiter-ingest-worker');
    end if;
    if exists (select 1 from cron.job where jobname = 'recruiter-retention') then
        perform cron.unschedule('recruiter-retention');
    end if;
end;
$$;

-- Finds inboxes whose next_sync_at is due and enqueues ingestion_jobs.
select cron.schedule(
    'recruiter-scheduler',
    '* * * * *',
    $$select public.invoke_edge_function('scheduler', '{"source":"pg_cron"}'::jsonb);$$
);

-- Claims queued/retry ingestion_jobs and processes them.
select cron.schedule(
    'recruiter-ingest-worker',
    '* * * * *',
    $$select public.invoke_edge_function('ingest-worker', '{"source":"pg_cron"}'::jsonb);$$
);

-- Housekeeping: drop chat noise and executed approvals older than 90 days.
select cron.schedule(
    'recruiter-retention',
    '15 3 * * *',
    $$
    delete from public.chat_messages
     where created_at < now() - interval '90 days';
    delete from public.pending_actions
     where status in ('executed', 'failed', 'expired')
       and created_at < now() - interval '90 days';
    $$
);
