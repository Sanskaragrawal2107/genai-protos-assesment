# Setup (production only)

There is one supported topology: **Supabase Postgres + Supabase Auth + Supabase
Storage**, with optional Composio for real Gmail, Microsoft Teams and Google
Meet calls. The API validates every required setting at boot and refuses to
start if anything is missing. There is no demo mode, SQLite fallback, local
identity or simulated provider.

You can run this exact production configuration on your machine to test before
deploying.

## Prerequisites

- Python 3.11–3.13 (3.12 recommended)
- Node.js 18.18+ (20+ recommended)
- A Supabase project (free tier is fine)
- Optional: a Composio project, an OpenAI-compatible LLM endpoint, a GitHub token

## 1. Supabase project

1. Create a project at <https://supabase.com/dashboard>.
2. Open **SQL Editor** and run, in order:
   - `supabase/migrations/0001_schema.sql`
   - `supabase/migrations/0002_rls.sql`
   - `supabase/migrations/0003_cron.sql` (optional; only for scheduled ingestion)
3. Confirm the private storage bucket exists:

```sql
select id, public from storage.buckets where id = 'resumes';
```

4. Create a user under **Authentication → Users** (email + password). The web
   app signs in with email and password; magic links are not used.

### Credentials to copy

| Value | Where to find it |
| --- | --- |
| `SUPABASE_URL` | Project Settings → API → Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API → `service_role` / secret key (server only) |
| `SUPABASE_JWT_SECRET` | Project Settings → API → JWT Settings → JWT Secret (legacy HS256). Used only for symmetric tokens. |
| `DATABASE_URL` | Project Settings → Database → Connection string → URI (Session pooler) |

`DATABASE_URL` must use the asyncpg driver. Convert the copied URI:

```
postgresql+asyncpg://postgres.<project-ref>:<url-encoded-password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Use the **session pooler** (port `5432`) or a direct connection. If you use the
transaction pooler (port `6543`), append `?prepared_statement_cache_size=0`.

> Access tokens are verified by their `alg` header: HS256 against the legacy
> JWT secret, ES256/RS256 against the project's JWKS at
> `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`. New Supabase projects issue
> asymmetric tokens by default, so both schemes are supported. The verifier
> needs the `cryptography` package (listed in `requirements.txt`).

## 2. API environment

Copy `.env.example` to `.env` at the repository root and fill in:

```bash
WEB_ORIGIN=http://localhost:3000          # exact web origin; change when deployed
API_INTERNAL_TOKEN=<64-char random hex>   # shared with Edge Functions

DATABASE_URL=postgresql+asyncpg://...     # Supabase Postgres
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
SUPABASE_JWT_SECRET=<legacy-jwt-secret>
SUPABASE_STORAGE_BUCKET=resumes
```

Generate the internal token:

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

`API_INTERNAL_TOKEN` must not be empty or a known placeholder; the API rejects
weak values.

Optional values:

```bash
OPENAI_API_KEY=...                        # enables LLM extraction/drafting
COMPOSIO_API_KEY=...                      # enables real Gmail + Teams/Google Meet
GITHUB_TOKEN=...                          # raises GitHub API rate limit
```

## 3. API

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Swagger: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>
- Startup logs confirm `db=postgres | llm=... | composio=... | storage=supabase`.

The API reads the root `.env` (and `apps/api/.env` if present). It does not
create tables at boot; the SQL migrations are authoritative.

## 4. Web environment

Create `apps/web/.env.local` from `apps/web/.env.local.example`:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon-key>
```

Only public values go here. Never put the service-role key or Composio key in
the web environment.

## 5. Web

```bash
cd apps/web
npm install
npm run dev              # http://localhost:3000
# or production-like:
npm run build
npm run start
```

Open <http://localhost:3000>. You will be redirected to `/login`; sign in with
the Supabase user you created. The first authenticated request bootstraps a
workspace and a membership row for that user.

## 6. Connect Gmail, Teams and Google (Composio)

This is required for real resume ingestion, email sending and meeting links.

1. Create a Composio project and add the **Gmail**, **Microsoft Teams**,
   **Google Calendar** and **Google Meet** toolkits.
2. Set `COMPOSIO_API_KEY` in the root `.env`, then restart the API. Auth configs
   are resolved from the Composio project, or created as Composio-managed
   configs on first connect — there are no auth-config IDs to configure.
3. In the web app open **Settings → Integrations**, press **Connect** for each
   provider you intend to use, complete OAuth in the new tab, then press
   **Verify**.
4. Confirm each connector shows `active` and the expected account address.

You only need one meeting provider:

- **Microsoft Teams** — Microsoft 365 Work/School accounts with Teams enabled.
- **Google Calendar & Meet** — creates the calendar event *and* its Meet link.
  Google Workspace accounts always get a Meet link; personal Gmail accounts may
  not (Google silently omits the conference).
- **Google Meet (standalone)** — creates a real Meet room without a calendar
  event. Use it when Google Calendar cannot attach Meet rooms, or as an extra
  connection alongside Google Calendar.

Then choose the workspace default under **Settings → Integrations → Meeting
provider** (`Microsoft Teams`, `Google Meet`, or none). Every scheduling request
can override it.

Install the SDK if you created the venv before adding it to requirements:

```bash
.venv/Scripts/pip install "composio>=0.31"
```

Without Composio the product still runs, but Gmail ingestion and interview
scheduling fail honestly with "not connected" / "not configured" errors.

## 7. Optional: scheduled ingestion

By default the API runs the scheduler in-process: on startup it starts a loop
that calls `scheduler.tick()` every `SCHEDULER_TICK_SECONDS` (default 60). This
is the simplest working setup for a single instance:

```bash
SCHEDULER_INPROCESS=true      # default
SCHEDULER_TICK_SECONDS=60
```

For the database-driven alternative, set `SCHEDULER_INPROCESS=false` and let
`pg_cron` call the two Edge Functions every minute:

```bash
supabase functions deploy scheduler     --project-ref <ref>
supabase functions deploy ingest-worker --project-ref <ref>

supabase secrets set \
  API_BASE_URL=https://api.your-domain.com \
  API_INTERNAL_TOKEN=<same value as the API> \
  --project-ref <ref>
```

Then create the two Vault secrets used by `invoke_edge_function()`:

```sql
select vault.create_secret('https://<ref>.supabase.co', 'supabase_project_url');
select vault.create_secret('<service-role-key>', 'supabase_service_role_key');
```

For a purely local API, keep the default in-process scheduler (no Edge
Function can reach `127.0.0.1`). You can also trigger one tick manually:

```bash
curl -X POST http://127.0.0.1:8000/internal/cron/tick \
  -H "Authorization: Bearer $API_INTERNAL_TOKEN"
```

## 8. Optional: MCP server

```bash
cd apps/mcp
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python server.py          # stdio
.venv/Scripts/python server.py --http   # http://127.0.0.1:8765/mcp
```

It reads the same root `.env`. Set `MCP_WORKSPACE_ID` to pin a workspace and
`MCP_USER_ID` to the audit identity. See [`apps/mcp/README.md`](../apps/mcp/README.md).

## 9. Tests

The suite requires a real Postgres scratch database (never the application
database — it drops and recreates every table):

```bash
cd apps/api
TEST_DATABASE_URL=postgresql+asyncpg://... \
.venv/Scripts/python -m pytest -q
```

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| API exits at startup with "misconfigured" | Fill every required value listed in the error; `DATABASE_URL` must start with `postgresql+asyncpg://`. |
| API cannot connect to Postgres | Use the session pooler (5432), URL-encode the password, or add `?prepared_statement_cache_size=0` for the transaction pooler (6543). |
| `401 Invalid access token` | Token signature does not verify: check `SUPABASE_URL` (JWKS) for ES256/RS256 tokens, or `SUPABASE_JWT_SECRET` for legacy HS256 tokens. |
| Browser requests blocked by CORS | `WEB_ORIGIN` must exactly match the browser origin (scheme, host, port). |
| `503 Authentication is not configured` | Neither `SUPABASE_URL` nor `SUPABASE_JWT_SECRET` is set. |
| Scheduling fails with "Microsoft Teams is not connected" or "Google Meet is not connected" | Connect that provider under Settings → Integrations, or pick a different meeting provider. The product refuses to fake a join link. |
| Scheduling fails with "Google did not return a Meet link" | The Google account is a personal Gmail, so Calendar cannot attach a Meet room. Connect **Google Meet (standalone)** or use a Google Workspace account, or choose Microsoft Teams. |
| Google Calendar events have no Meet link | Expected on personal Gmail accounts. Connect the standalone Google Meet toolkit; the product patches the real Meet link onto the event. |
| `422` from `POST /api/connectors/connect` | Use `gmail`, `microsoft_teams`, `google_calendar` or `google_meet` (raw `googlecalendar`/`googlemeet` are accepted too). |
| `503` from an Edge Function | `API_BASE_URL` / `API_INTERNAL_TOKEN` secrets are not set on the function. |
| Cron jobs log a notice and do nothing | The Vault secrets `supabase_project_url` / `supabase_service_role_key` are missing. |
| No candidates after "Sync now" | No Gmail account connected, or the connected inbox has no resume attachments. Check **Settings → Sync → Recent ingestion jobs**. |
| `Cannot apply unknown utility class` when building the web app | Tailwind v4 needs custom classes declared with `@utility`, not `@layer components` (already fixed in `globals.css`). |
