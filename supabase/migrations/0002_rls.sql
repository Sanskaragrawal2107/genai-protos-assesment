-- ============================================================================
-- 0002_rls.sql — row level security
--
-- Tenancy model:
--   * every product row carries workspace_id (text);
--   * workspace_members maps a Supabase auth user (auth.uid()::text) to a
--     workspace;
--   * the FastAPI service talks to Postgres with the service role, which
--     bypasses RLS. Browser/anon clients (Supabase JS) are restricted to the
--     workspaces they belong to.
--
-- Path convention for the private ``resumes`` bucket:
--   resumes/<workspace_id>/<candidate_id>/<filename>
-- ============================================================================

create or replace function public.is_workspace_member(target_workspace text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1
        from public.workspace_members m
        where m.workspace_id = target_workspace
          and m.user_id = auth.uid()::text
    );
$$;

revoke all on function public.is_workspace_member(text) from public;
grant execute on function public.is_workspace_member(text) to authenticated;

-- --------------------------------------------------------------- workspaces --
alter table public.workspaces enable row level security;
drop policy if exists workspaces_member_access on public.workspaces;
create policy workspaces_member_access on public.workspaces
    for all to authenticated
    using (public.is_workspace_member(id))
    with check (public.is_workspace_member(id));

alter table public.workspace_members enable row level security;
drop policy if exists workspace_members_member_access on public.workspace_members;
create policy workspace_members_member_access on public.workspace_members
    for all to authenticated
    using (public.is_workspace_member(workspace_id))
    with check (public.is_workspace_member(workspace_id));

-- --------------------------------------------- every workspace-scoped table --
do $$
declare
    target text;
begin
    foreach target in array array[
        'connected_accounts',
        'inbox_sync_settings',
        'ingestion_jobs',
        'email_messages',
        'email_attachments',
        'candidates',
        'jobs',
        'candidate_applications',
        'candidate_evaluations',
        'candidate_communications',
        'interviews',
        'candidate_status_history',
        'recruiter_notes',
        'recruiter_activity_logs',
        'pending_actions',
        'chat_threads',
        'chat_messages'
    ] loop
        execute format('alter table public.%I enable row level security', target);
        execute format('drop policy if exists %I on public.%I', target || '_member_access', target);
        execute format(
            'create policy %I on public.%I for all to authenticated '
            'using (public.is_workspace_member(workspace_id)) '
            'with check (public.is_workspace_member(workspace_id))',
            target || '_member_access', target
        );
    end loop;
end;
$$;

-- ------------------------------------------------------------- storage RLS --
-- Storage objects are owned by Supabase's storage schema; policies live there.
drop policy if exists resumes_member_select on storage.objects;
create policy resumes_member_select on storage.objects
    for select to authenticated
    using (
        bucket_id = 'resumes'
        and public.is_workspace_member((storage.foldername(name))[1])
    );

drop policy if exists resumes_member_insert on storage.objects;
create policy resumes_member_insert on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'resumes'
        and public.is_workspace_member((storage.foldername(name))[1])
    );

drop policy if exists resumes_member_update on storage.objects;
create policy resumes_member_update on storage.objects
    for update to authenticated
    using (
        bucket_id = 'resumes'
        and public.is_workspace_member((storage.foldername(name))[1])
    )
    with check (
        bucket_id = 'resumes'
        and public.is_workspace_member((storage.foldername(name))[1])
    );

drop policy if exists resumes_member_delete on storage.objects;
create policy resumes_member_delete on storage.objects
    for delete to authenticated
    using (
        bucket_id = 'resumes'
        and public.is_workspace_member((storage.foldername(name))[1])
    );
