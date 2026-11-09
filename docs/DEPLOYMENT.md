# Deployment

The production topology is:

```
Browser ─▶ Next.js (web) ─▶ FastAPI (api) ─▶ Supabase Postgres (+ Storage)
                                   ▲
   Supabase pg_cron ─▶ Edge Functions (scheduler, ingest-worker) ─▶ API /internal/*
```

The API holds the service-role key and all domain logic; Edge Functions are thin
authenticated relays; `pg_cron` is the heartbeat.

> Apply the migrations to your own Supabase project. They are the only source of
> truth for the schema, RLS policies, storage bucket and cron jobs.

## 1. Supabase database

### Apply the migrations

Migrations are plain SQL and idempotent where practical:

| File | Contents |
| --- | --- |
| `supabase/migrations/0001_schema.sql` | 19 tables, indexes, `set_updated_at()` triggers, private `resumes` bucket. |
| `supabase/migrations/0002_rls.sql` | `is_workspace_member()` + per-table RLS + storage RLS. |
| `supabase/migrations/0003_cron.sql` | `pg_cron`/`pg_net`, `invoke_edge_function()`, three scheduled jobs. |

Either use the Supabase CLI:

```bash
supabase link --project-ref <your-ref>
supabase db push
```

…or paste each file into the SQL editor / apply it through a migration tool.
`0001` must run before `0002`; `0003` last.

> Note: `candidates.current_role` is a reserved word and is quoted as
> `"current_role"` in `0001_schema.sql`. Keep the quotes.

### Create a workspace + member

The API bootstraps a workspace on first authenticated request, but for RLS you
need membership rows so browser clients can read directly:

```sql
insert into public.workspace_members (workspace_id, user_id, email, role)
values ('<workspace_id>', '<supabase auth user id>', '<email>', 'owner')
on conflict do nothing;
```

### Connection string

Use the **pooled** connection string (port `6543`) for the API:

```
DATABASE_URL=postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
```

The SQL migrations are authoritative; the API never creates tables at boot.

## 2. API

Deploy `apps/api` to any container host (Render, Fly.io, Cloud Run, a VM…).

Environment (see [`.env.example`](../.env.example)):

```bash
DATABASE_URL=postgresql+asyncpg://...           # Supabase Postgres (asyncpg)
API_INTERNAL_TOKEN=<long random secret>         # shared with the Edge Functions
WEB_ORIGIN=https://app.your-domain.com
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server secret>
SUPABASE_JWT_SECRET=<legacy HS256 JWT secret>    # optional; ES256/RS256 verified via JWKS
COMPOSIO_API_KEY=...
# No auth-config IDs: configs are resolved or created per toolkit on first connect.
```

The API validates this configuration at boot and exits if anything required is
missing or weak.

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The API runs the scheduler tick in-process (`SCHEDULER_INPROCESS`, default
`true`), so a single long-running instance keeps automatic ingestion working
with no extra infrastructure. The idempotent `/internal/cron/tick` endpoint
stays available for pg_cron (section 5); set `SCHEDULER_INPROCESS=false` before
enabling that path so two schedulers do not run at once.

## 3. Web

Deploy `apps/web` to Vercel (or any Node host):

```bash
NEXT_PUBLIC_API_URL=https://api.your-domain.com
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon key>
```

`npm run build && npm run start`. Only public values go here — never the
service-role key.

## 4. Edge Functions

Two functions relay to the API. Deploy with the CLI:

```bash
supabase functions deploy scheduler     --project-ref <ref>
supabase functions deploy ingest-worker --project-ref <ref>
```

Then set their secrets (**API base URL + the internal token**):

```bash
supabase secrets set \
  API_BASE_URL=https://api.your-domain.com \
  API_INTERNAL_TOKEN=<same value as the API> \
  --project-ref <ref>
```

Both are deployed with `verify_jwt=true`, so only a privileged caller (the
service-role JWT used by `pg_cron`) can invoke them. Without the secrets they
return `503` with a descriptive message — they never pretend to have run.

## 5. Optional: scheduled jobs (pg_cron)

By default the API schedules ingestion itself (`SCHEDULER_INPROCESS=true`).
The pg_cron path is an alternative for deployments that prefer the database to
drive the heartbeat: set `SCHEDULER_INPROCESS=false`, then `0003_cron.sql`
schedules three jobs:

| Job | Schedule | Action |
| --- | --- | --- |
| `recruiter-scheduler` | every minute | POST `scheduler` → API `/internal/cron/tick` (enqueue due inboxes + drain jobs) |
| `recruiter-ingest-worker` | every minute | POST `ingest-worker` → API `/internal/recover` then `/internal/cron/tick` |
| `recruiter-retention` | daily 03:15 | prune chat messages and executed approvals older than 90 days |

The jobs call `public.invoke_edge_function()`, which reads the project URL and
service-role key from **Vault**. Create those two secrets once:

```sql
select vault.create_secret('https://<ref>.supabase.co', 'supabase_project_url');
select vault.create_secret('<service-role-key>', 'supabase_service_role_key');
```

Until both exist, the jobs are still scheduled but log a notice and make no
network call — by design, nothing is faked.

Verify in `cron.job_run_details`:

```sql
select jobid, status, return_message, start_time
from cron.job_run_details
order by start_time desc
limit 20;
```

## 6. Post-deploy checklist

- [ ] Migrations applied; `select count(*) from storage.buckets where id='resumes'` returns 1.
- [ ] API reachable over HTTPS and boots with production validation passing; `DATABASE_URL` points at Supabase Postgres.
- [ ] `API_INTERNAL_TOKEN` identical in the API and both Edge Functions.
- [ ] Vault secrets created; `cron.job` shows 3 jobs; `job_run_details` shows successes. (Or leave pg_cron disabled and confirm `/health` reports `"scheduler":"in-process"`.)
- [ ] Web build points at the deployed API and Supabase project.
- [ ] Settings → Integrations shows Gmail plus at least one meeting provider (Microsoft Teams or Google Calendar/Meet) `active` after OAuth.
- [ ] A test interview produces a real Teams join URL or Google Meet link; Google events are visible on the connected calendar and the Gmail draft is queued.
- [ ] `POST /api/connectors/connect` returns `200` for `google_calendar` / `google_meet` (no `422`).

## Rollback / rotation

- **Token rotation:** change `API_INTERNAL_TOKEN` in the API and re-run
  `supabase secrets set`; no redeploy of cron is needed.
- **Service-role rotation:** update the Vault secret and the API env together.
- **Schema changes:** add a new numbered migration; never edit an applied one.
