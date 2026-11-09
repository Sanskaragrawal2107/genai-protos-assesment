# Architecture

## Bird's-eye view

```
                    ┌─────────────────────────────┐
   recruiter ──────▶│  apps/web  (Next.js 15)     │
                    │  Supabase auth + RLS reads  │
                    └──────────────┬──────────────┘
                                   │ REST (Bearer JWT)
                                   ▼
                    ┌─────────────────────────────┐
                    │  apps/api  (FastAPI)        │
                    │  ─ all domain logic         │
                    │  ─ service-role DB access   │
                    │  ─ cron tick endpoint       │
                    └───┬───────────┬─────────────┘
                        │           │
          Composio SDK  │           │  SQLAlchemy (asyncpg)
        (tools + MCP)   │           │
                        ▼           ▼
             ┌───────────────┐  ┌──────────────────┐
             │Gmail/Teams/   │  │ Postgres (Supabase)│
             │Google+Composio│  │  19 tables + RLS   │
             └───────────────┘  └──────────────────┘
                                   ▲        ▲
                    Edge Functions │        │ pg_cron
                  (thin relays) ───┘        └── schedules
                                   ▲
                                   │ MCP (stdio/HTTP)
                    ┌──────────────┴──────────────┐
                    │  apps/mcp  (FastMCP)        │
                    │  same service layer         │
                    │  + Composio hosted-MCP proxy│
                    └─────────────────────────────┘
```

## Components

### `apps/api` — FastAPI service

| Area | Files | Responsibility |
| --- | --- | --- |
| Entrypoint | `app/main.py` | FastAPI app, CORS, lifespan (validate config, verify DB), error handlers. |
| Config | `app/config.py` | Pydantic settings from `.env`; capability flags (`llm_enabled`, `composio_enabled`). |
| Schema (ORM mirror) | `app/db.py` | SQLAlchemy Core tables — the authoritative shape mirrored by the SQL migrations. |
| REST API | `app/routers/api.py` | All product endpoints (candidates, jobs, interviews, communications, actions, chat, settings). |
| Internal API | `app/routers/internal.py` | Token-authenticated worker endpoints (`/internal/cron/tick`, `/internal/recover`) for cron/edge relays. |
| Security | `app/security.py` | Supabase JWT verification and the internal-token dependency. |
| Repo helpers | `app/repo.py` | Workspace bootstrap, candidate queries, pending actions, JSON-safe serialisation. |
| Workflows | `app/graphs/*` | LangGraph-style state machines for ingestion, evaluation, scheduling and the assistant. |
| Services | `app/services/*` | Resume parsing, evaluation, verification, calendar, interviews, communications, ingestion, scheduler, storage, dashboard. |
| Integrations | `app/composio.py`, `app/services/composio_mcp.py` | Official Composio SDK adapter + per-workspace MCP session helpers. |

### `apps/web` — Next.js dashboard

- App Router pages: `/` (dashboard), `/assistant`, `/candidates`, `/candidates/[id]`,
  `/shortlists`, `/jobs`, `/jobs/[id]` (ranked evaluation), `/interviews`,
  `/communications`, `/login` (Supabase auth),
  `/settings/integrations`, `/settings/sync`, `/settings/team`.
- `lib/api.ts` attaches the Supabase access token; `app/providers.tsx` resolves
  the session and the `/api/me` workspace.
- Reads are workspace-scoped through the API. Direct Supabase reads (if used)
  are constrained by RLS; the service-role key is never shipped to the browser.

### `apps/mcp` — FastMCP server

Thin wrappers over the **same** service functions, so the LLM gets typed,
DB-backed tools instead of guessing. External side effects require
`confirm=true`. When Composio is configured it also mounts the hosted Gmail,
Teams and Google MCP tools under the `composio` namespace.

### `supabase`

- `migrations/0001_schema.sql` — 19 public tables, indexes, updated-at triggers,
  the private `resumes` bucket.
- `migrations/0002_rls.sql` — `is_workspace_member()` plus per-table policies and
  storage RLS on the `resumes` bucket.
- `migrations/0003_cron.sql` — `pg_cron` + `pg_net`, the `invoke_edge_function()`
  helper (reads secrets from Vault) and three jobs.
- `functions/scheduler`, `functions/ingest-worker` — authenticated relays that
  call the API's internal endpoints. They are deliberately thin: no domain logic.

## Data model

| Table | Purpose |
| --- | --- |
| `workspaces`, `workspace_members` | Tenancy. Every other row carries `workspace_id`. |
| `connected_accounts` | A provider connection (Gmail / Microsoft Teams / Google Calendar / Google Meet) with its Composio connected-account id. |
| `inbox_sync_settings` | Per-inbox cadence: interval, Gmail query, next sync, failure streak. |
| `ingestion_jobs` | Queue + retry state for each ingestion run. |
| `email_messages`, `email_attachments` | Ingested messages and their resume attachments (with storage paths + hashes). |
| `candidates` | Structured candidate profile (skills, experience, projects, links, resume text/confidence). |
| `jobs` | Job description + structured requirements. |
| `candidate_applications` | Links a candidate to the message/job they applied through. |
| `candidate_evaluations` | One row per (candidate, job): component scores, weights, explanation, evidence. |
| `candidate_communications` | Drafts, queued and sent outbound email. |
| `interviews` | Meeting provider (Teams / Google Meet), calendar event ids, join URL, invite state, idempotency key, attendees. |
| `candidate_status_history`, `recruiter_notes`, `recruiter_activity_logs` | Audit + collaboration. |
| `pending_actions` | The approval queue for external side effects. |
| `chat_threads`, `chat_messages` | Assistant conversations. |

## Key flows

### 1. Resume ingestion

```
API process (in-process scheduler, default) ─┐
                                              ├─▶ tick(): find inbox_sync_settings
pg_cron ─▶ Edge Function "scheduler" ────────┘     where next_sync_at <= now
             ─▶ POST /internal/cron/tick            and the account is active
              ─▶ enqueue ingestion_jobs ─▶ process job
              ─▶ Composio GMAIL_FETCH_EMAILS (quad-based query)
              ─▶ GMAIL_GET_ATTACHMENT ─▶ store in resumes bucket
              ─▶ parse resume ─▶ upsert candidate ─▶ record application
              ─▶ schedule next_sync_at (progress or exponential backoff)
```

Idempotency is enforced by `uq_email_provider_id` (per account + message id) and
`uq_attachment_provider_id`. Failures increment `attempts` and back off; a
crashed worker's rows are reclaimed by `/internal/recover`.

### 2. Evaluation

```
POST /api/jobs ─▶ structured requirements
POST /api/jobs/{id}/evaluate
   retrieve candidate pool ─▶ (optional) GitHub/LeetCode verification
   ─▶ component scores (skills .38, experience .20, projects .22, location .08, evidence .12)
   ─▶ written explanation + strengths + missing requirements + concerns
   ─▶ upsert candidate_evaluations (unique per candidate+job)
```

Scoring is deterministic and reproducible; an LLM only enriches the narrative,
never the component arithmetic.

### 3. Interview scheduling

```
POST /api/interviews/schedule  ─▶ create pending action (approval gate)
POST /api/actions/{id}/approve
   ─▶ Microsoft Teams: create online meeting
        MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING (fallback CREATE_MEETING)
   ─▶ Google Meet: create the calendar event + room
        GOOGLECALENDAR_CREATE_EVENT (create_meeting_room, hangoutLink)
        fallback GOOGLEMEET_CREATE_MEET for accounts that cannot attach Meet
   ─▶ persist the real join URL + calendar event id on the interview row
   ─▶ queue a Gmail confirmation draft + send action
POST /api/actions/{id}/approve (send_email) ─▶ GMAIL_SEND_EMAIL
```

Both providers are first-class: `provider` is `microsoft_teams | google_meet |
none` and the recruiter can override the workspace default per interview. A
missing join URL is an error — no room is ever synthesised. Conflict detection
(`availability()`) only looks at `interviews` in the same workspace.

### 4. Assistant

`POST /api/chat` runs the assistant graph: classify intent → run the matching
node (search / compare / draft / schedule / approvals) → return
`{thread_id, message, reply, intent, candidates, data, pending_actions}`.
Side-effecting nodes write `pending_actions` instead of acting directly.

## Security model

- **Authentication.** Browser clients present a Supabase JWT; the API verifies it
  (`app/security.py`). The internal endpoints use a constant-time compare against
  `API_INTERNAL_TOKEN`.
- **Authorisation.** Every query is filtered by `workspace_id`. Postgres RLS
  (`is_workspace_member`) protects any direct Supabase access; the service role
  used by the API bypasses RLS by design.
- **Secrets.** Composio API key, MCP auth headers and the service-role key live
  only server-side. `public_view()` strips credentials before anything leaves the
  server.
- **Approval gate.** `pending_actions` is the single choke point for emails,
  meeting creation and cancellation.
- **Untrusted input.** Resume and email text is fenced before being sent to the
  LLM (`app/llm.py`).

## Failure philosophy

Every integration boundary fails loudly and honestly:

- Missing Composio config → endpoints report `configured: false` with the missing
  variables; scheduling raises `IntegrationError` rather than inventing a link.
- Missing cron Vault secrets → `invoke_edge_function()` raises a notice and does
  nothing.
- Unconfigured Edge Function secrets → the function returns `503` with a
  descriptive message.
