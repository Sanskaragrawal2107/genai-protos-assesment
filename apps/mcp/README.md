# AI Recruiter OS — MCP server

FastMCP server that exposes the **whole recruiting database and the AI workflows**
to any MCP client (Claude Desktop, Cursor, VS Code, n8n…). It reuses the exact
same service layer as the FastAPI app (`apps/api/app`), so there is one source of
truth for validation, scoring, verification and scheduling.

## Why MCP here

The LLM does not need to guess locations, skills or experience: it calls
typed tools such as `find_candidates(location="Bengaluru", min_experience=2)`
and receives rows straight from Supabase Postgres. Filters are executed in the
database, not hallucinated.

## Tools

| Tool | Purpose |
| --- | --- |
| `workspace_overview` | dashboard counters, recent ingestion, upcoming interviews |
| `list_candidates` | filter by status/location/skills/experience |
| `get_candidate` | full profile, communications, evaluations, history |
| `update_candidate_status` | move through the pipeline (audited) |
| `add_candidate_note` | recruiter note + next follow-up date |
| `list_jobs` / `create_job` | job descriptions |
| `evaluate_candidates` | structured JD → retrieval → GitHub/LeetCode → ranking |
| `compare_candidates` | side-by-side score breakdown for a job |
| `verify_candidate` | GitHub + LeetCode evidence with source links |
| `get_sync_status` / `sync_inbox_now` | Gmail sync state and manual sync |
| `list_interviews` | scheduled interviews |
| `check_availability` | conflicts **within this workspace** + suggested slots (external calendars are never assumed) |
| `schedule_interview` | approval-gated; creates a real Microsoft Teams or Google Meet room + Gmail invitation |
| `reschedule_interview` / `cancel_interview` | existing interviews (Teams meeting / Google Calendar event updated or deleted, Gmail notice queued) |
| `draft_email` | personalised draft (LLM or template), saved as pending |
| `send_email` | sends through Gmail only with `confirm=true` |
| `list_pending_actions` / `approve_action` / `reject_action` | approval queue |
| `composio_status` | tool-layer configuration + connected accounts for this workspace |
| `composio_session` | create/reuse the workspace's hosted Composio MCP session (browser-safe summary) |

### Composio hosted MCP bridge

When `COMPOSIO_API_KEY` + `MCP_WORKSPACE_ID` are set, the server mounts
Composio's hosted Gmail, Microsoft Teams and Google MCP under the `composio` namespace
(`composio/<tool>`). Credentials and the session URL stay server-side. If
Composio is not configured the bridge is simply not mounted — no fake tools.

> Composio sessions expire (about 25 minutes). A long-running bridge re-mounts
> on the next server start; call `composio_session()` to confirm the current
> session is live.

## Run

```bash
cd apps/mcp
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
python server.py            # stdio (for Claude Desktop / Cursor)
python server.py --http     # streamable HTTP on :8765/mcp
```

Environment: the server reads the same `.env` as the API (`DATABASE_URL`,
`COMPOSIO_API_KEY`, `OPENAI_API_KEY`, …) plus:

```bash
MCP_WORKSPACE_ID=            # pin a workspace
MCP_USER_ID=                 # audit identity; required when MCP_WORKSPACE_ID is empty
```

## Claude Desktop config

```json
{
  "mcpServers": {
    "ai-recruiter": {
      "command": "C:/path/to/apps/mcp/.venv/Scripts/python.exe",
      "args": ["C:/path/to/apps/mcp/server.py"],
      "env": { "MCP_WORKSPACE_ID": "<workspace-id>" }
    }
  }
}
```

## Safety

* Every tool is workspace-scoped; the workspace comes from the server environment,
  never from model input.
* Tools that produce external side effects (`send_email`, `schedule_interview`,
  `cancel_interview`) require `confirm=true` and are recorded in
  `pending_actions` + `recruiter_activity_logs`.
* Resume and email content is untrusted input: it is fenced before being sent to
  the LLM (see `app/llm.py`).
