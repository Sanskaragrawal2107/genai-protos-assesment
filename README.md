# AI Recruiter OS

An AI-powered recruitment operating system: it reads candidate resumes from
**Gmail**, extracts structured profiles, ranks them against a job description
with an **explainable** score, verifies public evidence on **GitHub/LeetCode**,
lets a recruiter converse with the pipeline through an assistant, and books
interviews on **Microsoft Teams or Google Meet** with a **Gmail** invitation —
every external side effect gated behind human approval.

Production-only: the API runs against **Supabase Postgres**, authenticates
**Supabase JWTs**, stores resumes in **Supabase Storage**, and validates every
required setting at boot. There is no demo mode, no SQLite fallback, no local
identity and no simulated provider success.

Monorepo layout:

```
apps/api     FastAPI service — all domain logic, REST API, cron tick endpoint
apps/web     Next.js 15 dashboard (App Router, Tailwind v4, Supabase Auth)
apps/mcp     FastMCP server — the same services exposed as MCP tools
supabase     SQL migrations (schema, RLS, pg_cron) + Edge Functions
docs         Architecture, setup, deployment and Composio notes
```

## What it does

| Capability | How |
| --- | --- |
| **Resume ingestion** | Gmail is polled on a schedule. Resume attachments (PDF/DOCX/TXT) are downloaded, stored in a private bucket, and parsed into structured candidate rows. |
| **Explainable evaluation** | A job description is turned into structured requirements, candidates are retrieved from the DB, then scored on skills / experience / projects / location / evidence with a written explanation and named strengths & gaps. |
| **Verification** | GitHub (repos, languages, activity) and LeetCode profiles are fetched and cited as evidence — never invented. |
| **Recruiter assistant** | Natural-language search, compare, draft, and schedule over the live workspace. Tool side effects are queued as pending actions. |
| **Interview scheduling** | A real Microsoft Teams meeting or Google Meet room (plus its Google Calendar event) is created; the real join URL is emailed through Gmail. Conflicts are detected against interviews **this product** created. |
| **Approvals & audit** | Emails, interview creation and cancellation are written to `pending_actions` and `recruiter_activity_logs`; nothing external happens until a human approves. |
| **Composio tool layer** | Official Composio SDK with per-workspace scoped sessions, and a hosted MCP bridge so the chatbot can call Gmail/Teams tools. |

## Design principles

- **No fabricated integrations.** If a provider is not connected, the UI says so
  and the action fails honestly. There is no simulated "success".
- **Calendar conflicts are local-only.** Availability checks only consider
  interviews created in this workspace; external calendar availability is never
  assumed.
- **Gmail is the only email channel.** The recruiter chooses Microsoft Teams or
  Google Meet per interview; the workspace default is set in
  Settings → Integrations.
- **Service-role credentials never reach the browser.** Browser clients use
  Supabase auth and are constrained by row-level security.
- **Deterministic by default.** With no LLM key the product still works
  end-to-end using rule-based extraction and templated scoring/emails.

## Prerequisites

- Python 3.11–3.13 (3.12 recommended; very new Python releases may lack wheels
  for the AI/Composio dependency tree)
- Node.js 18.18+ (20+ recommended)
- A Supabase project (Postgres, Auth, Storage)
- Optional: a Composio project (Gmail, Microsoft Teams, Google Calendar/Meet), an
  OpenAI-compatible LLM endpoint, a GitHub token

## Quick start (production configuration, run locally)

1. Create a Supabase project, then apply `supabase/migrations/0001_schema.sql`
   and `0002_rls.sql` in the SQL editor (or `supabase db push`). `0003_cron.sql`
   is optional and only needed for scheduled ingestion in the cloud.
2. Fill in the root `.env` (API) and `apps/web/.env.local` (web).
3. Start the API:

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

4. Start the web app (separate terminal):

```bash
cd apps/web
npm install
npm run dev                                          # http://localhost:3000
# production-like: npm run build && npm run start
```

5. Sign in with a Supabase Auth user. The first authenticated request creates the
   workspace automatically. Connect Gmail and Microsoft Teams under
   **Settings → Integrations**.

The API validates its configuration at boot and refuses to start if
`DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_JWT_SECRET`, `API_INTERNAL_TOKEN` or `WEB_ORIGIN` is missing.

Full instructions, including every credential: [`docs/SETUP.md`](docs/SETUP.md) ·
Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
Deployment: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) ·
Composio: [`docs/COMPOSIO.md`](docs/COMPOSIO.md) ·
MCP server: [`apps/mcp/README.md`](apps/mcp/README.md).

## Configuration

All configuration is environment driven; see [`.env.example`](.env.example) for
the API and [`apps/web/.env.local.example`](apps/web/.env.local.example) for the
web app. The important groups are runtime, database, Supabase, LLM (optional),
Composio (Gmail + Teams/Google Meet), GitHub (optional) and MCP.

## Tests

```bash
cd apps/api
TEST_DATABASE_URL=postgresql+asyncpg://... \
.venv/Scripts/python -m pytest -q
```

The suite requires a real Postgres scratch database and drops/recreates every
table, so never point `TEST_DATABASE_URL` at your application database. Tests use
fakes for Gmail, Teams and Storage, so they never call external providers.

## Honest limitations

- **Live OAuth and real Gmail/Teams calls are unverified** without Composio
  credentials. The code paths are implemented and unit-tested with fakes, but
  the end-to-end hosted calls have not been exercised here.
- **Please confirm your Composio project** — the exact tool slugs used are
  listed in [`docs/COMPOSIO.md`](docs/COMPOSIO.md); if your account exposes
  different slugs, adjust the adapter.
- **Supabase cron** does nothing until the Vault secrets and Edge Function
  secrets are set (see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)); it logs a
  notice instead of faking a run.
- **JWT verification supports both signing schemes.** Legacy HS256 tokens are
  verified with `SUPABASE_JWT_SECRET`; ES256/RS256 tokens (the default for new
  Supabase projects) are verified against the project's JWKS.
