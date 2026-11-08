# Composio integration

Composio is the **only** external tool layer. The backend never hand-rolls OAuth
or REST calls to Gmail, Microsoft Teams or Google: it uses the official
`composio` Python SDK (>= 0.31), and the chatbot reaches the same tools through
Composio's **hosted MCP**.

Everything Composio-related lives in two files:

- `apps/api/app/composio.py` — the async adapter over the SDK (tools, connected
  accounts, scoped MCP sessions).
- `apps/api/app/services/composio_mcp.py` — per-workspace session helpers and the
  browser-safe `public_view()`.

## Toolkits and tool slugs

Four toolkits are used: **`gmail`**, **`microsoft_teams`**, **`googlecalendar`**
and **`googlemeet`**. A workspace connects Gmail plus whichever meeting
provider(s) it needs.

| Slug | Used by | Purpose |
| --- | --- | --- |
| `GMAIL_FETCH_EMAILS` | `services/mail.py` | List inbox messages (quad-based Gmail query). |
| `GMAIL_GET_ATTACHMENT` | `services/mail.py` | Download a resume attachment. |
| `GMAIL_SEND_EMAIL` | `services/communications.py` | Send an approved outbound email. |
| `MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING` | `services/calendar.py` | Preferred: create/reuse the Teams meeting. |
| `MICROSOFT_TEAMS_CREATE_MEETING` | `services/calendar.py` | Fallback when the variant above is unavailable. |
| `MICROSOFT_TEAMS_UPDATE_MEETING` | `services/calendar.py` | Reschedule. |
| `MICROSOFT_TEAMS_DELETE_USER_ONLINE_MEETING` | `services/calendar.py` | Cancel. |
| `GOOGLECALENDAR_CREATE_EVENT` | `services/calendar.py` | Create the calendar event + Meet room (`create_meeting_room`, returns `hangoutLink`). |
| `GOOGLECALENDAR_PATCH_EVENT` | `services/calendar.py` | Reschedule, or attach a standalone Meet link. |
| `GOOGLECALENDAR_DELETE_EVENT` | `services/calendar.py` | Cancel. |
| `GOOGLEMEET_CREATE_MEET` | `services/calendar.py` | Standalone real Meet space when Calendar cannot attach one. |

For Teams the provider tries the preferred slug and transparently falls back to
the alternate one; whichever succeeds, the real `joinWebUrl` is stored on the
interview row. For Google, the calendar event's `hangoutLink` (or the Meet
space's `meetingUri`) is stored. **No join URL is ever synthesised.**

> These slugs are the ones the adapter expects. If your Composio project exposes
> different names, adjust the constants in `services/calendar.py` and
> `services/mail.py`. The adapter surfaces a `tool_failed` / `not_found`
> `IntegrationError` rather than silently returning nothing, so a mismatch is
> obvious.

## Configuration

```bash
COMPOSIO_API_KEY=...
# optional
COMPOSIO_TOOLKIT_VERSION=
COMPOSIO_BASE_URL=https://backend.composio.dev
COMPOSIO_USER_PREFIX=airecruiter
```

No auth-config IDs are configured. At connect time the adapter calls
`composio.auth_configs.list(toolkit_slug=...)` and reuses the first enabled
config in your Composio project (custom or Composio-managed). When the project
has none, it creates one with `auth_configs.create(..., {"type":
"use_composio_managed_auth"})`. Adding a connector therefore stays a
Composio-dashboard concern: create a custom auth config there only if you want
to supply your own OAuth credentials; otherwise Composio-managed auth is
bootstrapped automatically.

## Connecting an account

1. **Settings → Integrations → Connect** (Gmail, Microsoft Teams, Google
   Calendar or Google Meet) calls `POST /api/connectors/connect`.
2. The API resolves or creates the toolkit's auth config, then calls
   `composio.connected_accounts.link(...)` and returns the OAuth URL; the
   browser opens it in a new tab.
3. After the user completes consent, **Verify** calls
   `POST /api/connectors/{id}/verify`, which reads
   `composio.connected_accounts.list(...)` and marks the row `active`, storing
   the `composio_connected_account_id` and the mailbox address.

A meeting connection (Teams, Google Calendar or Google Meet) is considered
usable only when the account row has a `composio_connected_account_id` and
status `active`. Availability does **not** depend on the global API key alone,
so the UI cannot show a green provider state without a real connection.

## How tools are executed

`app/composio.py` wraps the synchronous SDK with `asyncio.to_thread`:

```python
data = await composio.execute(
    "MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING",
    user_id=composio_user_id(workspace_id),
    arguments={...},
    connected_account_id=account_id,
)
```

- `tools.execute` → returns the tool's `data` payload.
- `auth_configs.list` / `create` → resolve or bootstrap the toolkit's auth config.
- `connected_accounts.link` / `list` → OAuth + status.
- Errors are mapped to `IntegrationError` with a machine-readable `code`
  (`not_configured`, `not_installed`, `already_connected`, `timeout`,
  `not_found`, `transport`, `provider_error`, `tool_failed`, `bad_response`).

## Hosted MCP for the chatbot

Each workspace gets its own Composio **user id**
(`<COMPOSIO_USER_PREFIX>:<workspace_id>`) and a scoped hosted-MCP session over
just Gmail, Teams and Google:

```python
session = await composio.create_mcp_session(
    user_id=composio_user_id(workspace_id),
    toolkits=["gmail", "microsoft_teams", "googlecalendar", "googlemeet"],
)  # manage_connections=True, mcp=True
# session.url, session.headers  ← credentials, server-side only
```

- Sessions are cached per user for 25 minutes and recreated on expiry.
- `GET /api/integrations/composio` returns a **browser-safe** summary:
  `configured`, `toolkits`, `user_id`, connected accounts and MCP availability.
  It never returns the session URL or its auth headers.
- `POST /api/integrations/composio/session` forces a fresh session for the
  workspace (still server-side).

### Mounting the MCP bridge (`apps/mcp`)

When `COMPOSIO_API_KEY` **and** `MCP_WORKSPACE_ID` are set, `apps/mcp/server.py`
mounts Composio's hosted MCP under the `composio` namespace using fastmcp's
`create_proxy` + `StreamableHttpTransport` (URL and headers from the session).
From the MCP client you then see `composio/*` tools alongside the native
recruiting tools.

If Composio is unconfigured, fastmcp lacks proxy support, or no workspace is
pinned, the bridge is **not mounted** — the server logs why and exposes
`composio_status()` instead of broken tools.

## Approval boundary

Composio calls are only made when a human has approved the corresponding
`pending_action`:

- **Email send** → `pending_actions.kind = "send_email"`.
- **Interview create** → `"schedule_interview"`.
- **Interview cancel** → `"cancel_interview"`.

Drafting an email or proposing an interview never calls Composio. Approving the
action is what triggers the tool execution and writes to
`recruiter_activity_logs`.

## Failure semantics

- No `COMPOSIO_API_KEY` → `composio_enabled` is false; `/api/integrations/composio`
  reports `configured: false` with the missing variables; scheduling raises an
  `IntegrationError` telling the user to connect Teams or Google.
- A toolkit not connected → `not_found` / typed error, surfaced in the UI, never
  a fabricated success.
- A key present but SDK missing → `not_installed` with a pip hint.

## Things that are intentionally not here

- **No synthetic meeting links.** Scheduling creates a real Teams join URL or a
  real Google Meet link (and Google Calendar event when connected). If the
  provider returns no link, the interview is not scheduled and the error is
  surfaced. External calendar availability is never read or assumed.
- **No service-role or Composio key in the frontend.** The browser only calls
  the API; the API holds every secret.
