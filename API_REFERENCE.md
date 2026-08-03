# Claude Remote Control API — Reverse-Engineered Reference

> **Unofficial.** Reconstructed from the Claude Code CLI (`/opt/claude-code/bin/claude`,
> a Bun single-file executable) and the API docs it embeds. These are private
> endpoints used by `claude.ai/code` and the Claude mobile apps. They are not a
> supported public interface and can change without notice. The list/read paths
> here were exercised against the live API; write/steer paths are documented from
> the client code.

---

## 1. The big picture

"Remote Control" lets `claude.ai/code` (web) and the Claude mobile app drive a
Claude Code session that is **running on your own machine**. The local `claude`
process registers as a **bridge** (a self-hosted, bring-your-own-compute
environment worker), keeps an outbound-only connection to Anthropic, and relays
its native event stream. The web/app is just a **controller** that sends messages
and reads the stream.

There are **two related HTTP surfaces**, both on `https://api.anthropic.com`:

| | **Remote Control** (mode A) | **Managed Agents** (mode B) |
|---|---|---|
| What runs Claude | **Your machine** (byoc) | Anthropic cloud |
| Session paths | `/v1/code/sessions/*` | `/v1/sessions/*` |
| Auth | claude.ai **OAuth Bearer** | **`x-api-key`** |
| `anthropic-beta` | `ccr-byoc-2025-07-29` | `managed-agents-2026-04-01` |
| Session id prefix | `cse_…` (alias `session_…`) | `sesn_…` |
| Event wire format | Claude Code **stream-json** (`user`/`assistant`/`result`/…) | dotted (`agent.message`/`session.status_idle`/…) |
| Send envelope | `{session_id, events:[{payload: <ev+uuid>}]}` | `{events:[<ev>]}` |
| Who creates the session | the local CLI (`claude remote-control`) | you, via `POST /v1/sessions` |

**To act like the webpage** (the goal of this project) you use **mode A** as a
*controller*: `GET /v1/code/sessions` → `POST …/events` → `GET …/events/stream`.
`ccr` = **C**laude **C**ode **R**emote control; `byoc` = **b**ring-**y**our-**o**wn-**c**ompute.

---

## 2. Authentication

### 2.1 Mode A — Remote Control (OAuth)

Requires a **claude.ai OAuth login** (Pro/Max/Team/Enterprise). API keys are *not*
accepted for Remote Control. The CLI stores tokens after `/login`:

**`~/.claude/.credentials.json`**
```json
{
  "claudeAiOauth": {
    "accessToken":  "<108-char OAuth token>",
    "refreshToken": "<108-char token>",
    "expiresAt":     1784158110498,          // epoch ms
    "refreshTokenExpiresAt": 1786217343498,
    "scopes": ["user:inference","user:profile","user:sessions:claude_code", ...],
    "subscriptionType": "max",
    "clientId": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
  }
}
```
The scope that gates Remote Control is **`user:sessions:claude_code`**.

**Organization UUID** (needed for the `x-organization-uuid` header) lives in
**`~/.claude.json`** → `oauthAccount.organizationUuid` (a top-level
`organizationUuid` in `.credentials.json` is used as a fallback).

**Request headers** (builder `DC(token)` + `Gnd`/`l$g`):
```
Authorization:              Bearer <accessToken>
Content-Type:               application/json
anthropic-version:          2023-06-01                 # the ONLY version string ever sent
anthropic-client-platform:  claude_code_remote         # XR(); web/mobile value
x-organization-uuid:        <organizationUuid>
anthropic-beta:             ccr-byoc-2025-07-29         # on /v1/sessions (v1) & /v1/code/* ; NOT on the bare /v1/code/sessions list
X-Trusted-Device-Token:     <token>                    # only if org requires Trusted Devices
User-Agent:                 claude-code/2.1.207         # any value works
```

**Token refresh** — `POST https://platform.claude.com/v1/oauth/token`
(note: **platform.claude.com**, not api.anthropic.com):
```
Content-Type: application/json
anthropic-beta: oauth-2025-04-20
User-Agent: anthropic-sdk-typescript/0.94.0 userOAuthProvider

{ "grant_type": "refresh_token", "refresh_token": "<...>",
  "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e" }
```
Response is standard OAuth (`access_token`, `refresh_token`, `expires_in`). Refresh
when `expiresAt <= now`. `client_id` overridable via `CLAUDE_CODE_OAUTH_CLIENT_ID`;
bare token via `CLAUDE_CODE_OAUTH_TOKEN`.

### 2.2 Mode B — Managed Agents (API key)
```
x-api-key:         <sk-ant-... API key>
anthropic-version: 2023-06-01
anthropic-beta:    managed-agents-2026-04-01
Content-Type:      application/json
```

### 2.3 `anthropic-beta` matrix
| beta | used for |
|---|---|
| `ccr-byoc-2025-07-29` | Mode A session/code endpoints |
| `managed-agents-2026-04-01` | Mode B endpoints |
| `oauth-2025-04-20` | OAuth token refresh |
| `files-api-2025-04-14` | `/v1/files` (both modes) |
| `oidc-federation-2026-04-01` | enterprise WIF/OIDC token exchange |

`anthropic-client-platform` (`XR()`) enum by `CLAUDE_CODE_ENTRYPOINT`:
`claude_code_remote` (web/mobile/desktop remote), `claude_code_vscode`,
`claude_code_sdk`, `claude_code_mcp`, `claude_code_github_action`, ….

---

## 3. Remote Control API (mode A — the controller surface)

Base: `https://api.anthropic.com`. All calls use the Mode-A headers from §2.1.
`{id}` is a `cse_…` session id.

### 3.1 Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/code/sessions` | List your Remote Control sessions |
| `GET` | `/v1/code/sessions/{id}` | Get one session |
| `PUT` | `/v1/code/sessions/{id}` | Update title — body `{"title": "..."}` |
| `POST` | `/v1/code/sessions/{id}/archive` | Archive/end (empty body `{}`; **200 or 409** = success) |
| `POST` | `/v1/code/sessions/{id}/mark_read` | Mark read (optional `{sequence_num}`) |
| `POST` | `/v1/code/sessions/{id}/client/presence` | Announce/clear a watching client |

There is **no DELETE** for code sessions — archive only. Session **creation** is done
by the local CLI, not the controller (see §5).

**`GET /v1/code/sessions` response** (verified live):
```json
{
  "data": [ { /* session object */ } ],
  "next_cursor": null,
  "resume_token": "..."
}
```

**Session object** (verified live):
```json
{
  "id": "cse_019B9e41WcXsxvZmtvJK9SEB",
  "title": "Add audio transcript functionality",
  "status": "active",                 // active | archived
  "status_bucket": "working",         // working | ...
  "worker_status": "running",         // idle | running | requires_action
  "connection_status": "connected",   // connected | connecting | ...
  "environment_id": "",
  "environment_kind": "bridge",       // Remote Control = "bridge"
  "config": { "model": "claude-opus-4-8", "mcp_connector_ids": [],
              "sources": [], "outcomes": [] },
  "created_at": "2026-07-15T15:25:49.307782Z",
  "last_event_at": "2026-07-15T15:53:22.309103Z",
  "user_message_count": "0",
  "tags": ["remote-control-repl"],
  "unread": true,
  "external_metadata": { "post_turn_summary": { "status_category": "review_ready",
                          "status_detail": "...", "needs_action": "" } },
  "participants": [], "relations": []
}
```
`requires_action` on `worker_status` means the agent is blocked on you (a permission
prompt); details live in `external_metadata.pending_action`.

**`POST …/client/presence`** — body `{client_id, connected_at}` (ISO-8601) to
announce, or `{client_id, clear:true}` to leave. The web client pulses this.

### 3.2 Sending events (steering)

`POST /v1/code/sessions/{id}/events`
```json
{ "session_id": "cse_...",
  "events": [ { "payload": { /* stream-json message, gets a uuid if missing */ } } ] }
```
Each event is wrapped in `payload`, and the client adds a `uuid` if the payload
lacks one. There is also a separate outbound channel `POST …/{id}/teleport-events`
(screen-mirroring) — not needed for steering.

**Send a user message** — payload is Claude Code stream-json, **not** `user.message`:
```json
{ "type": "user",
  "message": { "role": "user",
    "content": [ { "type": "text", "text": "run the tests" } ] } }
```
`content` accepts the full Messages-API block union: `text`,
`image` (`{source:{type:"base64",media_type,data}}`), `tool_result`, plus file
attachments.

**Steer via `control_request`** — payload `{type:"control_request", request_id, request:{subtype, ...}}`:

| `request.subtype` | effect |
|---|---|
| `interrupt` | stop the running agent (jumps the queue) |
| `set_model` (`model`) | change model mid-session |
| `set_permission_mode` (`mode`, opt `ultraplan`) | `default` \| `plan` \| `acceptEdits` \| `bypassPermissions` |
| `set_max_thinking_tokens` | thinking budget |
| `apply_flag_settings` (`settings`) | merge session flag settings — notably `settings.effortLevel` (`low` \| `medium` \| `high` \| `xhigh`, explicit `null` = back to auto) + `settings.ultracode` (bool). This is how the CLI's `/effort` reaches an SDK/cloud worker (there is **no** `set_effort` subtype; `max` is session-scoped and rejected by the worker's schema). ⚠️ **Remote-control REPL workers do NOT dispatch this subtype** (confirmed live on 2.1.187; the dispatch table is unchanged in 2.1.212) — they answer `control_response {subtype:"error", error:"REPL bridge does not handle control_request subtype: apply_flag_settings"}`. `RemoteControlClient.set_effort()` wraps it, with an optional wait-for-verdict + `/effort` slash-command fallback. |
| `can_use_tool` | answer a permission prompt |
| `mcp_message`, `request_user_dialog`, `seed_read_state`, `set_mcp_permission_mode_override` | other controls |

The remote-control REPL worker's full `control_request` dispatch table (2.1.187 and
2.1.212, from the binary): `initialize`, `set_model`, `set_max_thinking_tokens`,
`set_permission_mode`, `rename_session`, `set_color`, `file_suggestions`,
`read_file`, `get_context_usage`, `get_usage`, `mcp_status`, `mcp_authenticate`,
`mcp_oauth_callback_url`, `mcp_reconnect`, `interrupt` — anything else gets the
"does not handle" error response.

**Slash commands work as user messages** (confirmed live, 2026-07-17): a `user`
event whose text is e.g. `/effort high` is executed by the remote-control worker
as a **local command** — zero cost, `num_turns: 0`, no API call; the transcript
gains the synthetic `<command-name>` user events plus a synthetic assistant echo
of the command output (e.g. "Set effort level to high (saved as your default for
new sessions)"). Semantics are exactly "typed at that terminal" — so `/effort
<level>` **persists that machine's default**, it is not session-scoped. This is
the practical route for any steering the control table above lacks.

### 3.3 Receiving events

**Stream (SSE):** `GET /v1/code/sessions/{id}/events/stream?from_sequence_num=N`
- Each SSE frame: `event: client_event`, `id: <sequence_num>`,
  `data: {event_type, sequence_num, source, payload}`. The real event is `payload`.
- Other top-level frame kinds: `catch_up_truncated` (history-gap signal),
  `ephemeral_event`, and `session_update`/`delivery_update` (ignored by the CLI).
- **Resume** after a drop: reconnect with **both** `?from_sequence_num=<last>`
  **and** a `Last-Event-ID: <last>` header; de-dupe by `sequence_num`. Server-side
  resumable (unlike mode B). A liveness timer + exponential backoff drive reconnects.
- `sequence_num` arrives as a **string** on the wire — coerce to int.

**Poll (history):** `GET /v1/code/sessions/{id}/events?limit=N&sort_order=desc`
- `sort_order` = `asc` | `desc` (CLI fetches `desc` = newest first).
- Returns `{data:[{sequence_num, payload}, ...]}`. The CLI's paginator caps at 50 pages.

**stream-json payload `type` values** (the RC event vocabulary):

| `payload.type` | meaning |
|---|---|
| `system` (`subtype:"init"`) | session init: `cwd`, `session_id`, `tools[]`, `mcp_servers[]`, `model` |
| `system` (`subtype:"compact_boundary"`) | context was compacted |
| `user` | a user turn — `{message:{role:"user", content:[...]}}` (echoed sends + tool results) |
| `assistant` | assistant turn — `{message:{role:"assistant", content:[text|thinking|tool_use...]}}` |
| `result` | **turn complete** — `{subtype: success\|error_max_turns\|error_during_execution, is_error, num_turns, duration_ms, total_cost_usd, usage, stop_reason}` |
| `stream_event` | partial streaming (Messages-API `message_start`/`content_block_delta`/…) |
| `control_request` / `control_response` | the CLI control protocol (permissions, context usage, set_model, …) |
| `tool_progress`, `tool_use_summary`, `rate_limit_event`, `conversation_reset`, `active_goal`, `auth_status`, `env_manager_log`, `ephemeral_event` | auxiliary UI/telemetry events |

A turn ends on a **`result`** event (not `session.status_idle` — that's mode B).

---

## 4. Managed Agents API (mode B — cloud sessions)

Fully cloud-hosted. Auth = `x-api-key` + `managed-agents-2026-04-01`. The flow is
**create agent → create environment → create session → send/stream events**.
(The CLI's bundled SDK also appends `?beta=true` to these paths; the public curl
docs omit it — the header alone is sufficient.)

### 4.1 Endpoint map

**Agents** (`agent_…`; archive-only, no delete; each update makes a new version)
`GET|POST /v1/agents` · `GET|POST /v1/agents/{id}` · `POST /v1/agents/{id}/archive` · `GET /v1/agents/{id}/versions`

**Environments** (`env_…`; `config.type` = `cloud` | `self_hosted`)
`GET|POST /v1/environments` · `GET|POST|DELETE /v1/environments/{id}` · `POST /v1/environments/{id}/archive`
· self-hosted work queue: `GET /v1/environments/{id}/work/stats`, `POST /v1/environments/{id}/work/{work_id}/stop`

**Sessions** (`sesn_…`; both delete and archive)
`GET|POST /v1/sessions` · `GET|POST|DELETE /v1/sessions/{id}` · `POST /v1/sessions/{id}/archive`

**Events**
`GET|POST /v1/sessions/{id}/events` · `GET /v1/sessions/{id}/events/stream`
(opt into live text deltas with repeated `?event_deltas[]=agent.message|agent.thinking`)

**Threads** (per-subagent streams in multiagent sessions; `archive`-only)
`GET /v1/sessions/{id}/threads` · `GET /v1/sessions/{id}/threads/{tid}` ·
`POST /v1/sessions/{id}/threads/{tid}/archive` ·
`GET /v1/sessions/{id}/threads/{tid}/events` · `GET /v1/sessions/{id}/threads/{tid}/stream`

**Resources** (`sesrsc_…`; SDK method is `add`, delete-only)
`GET|POST /v1/sessions/{id}/resources` · `GET|POST|DELETE /v1/sessions/{id}/resources/{rid}`

**Files** (`file_…`; beta `files-api-2025-04-14`; delete-only)
`POST /v1/files` (multipart, `purpose=agent`) · `GET /v1/files[?scope_id={sesn}]` ·
`GET /v1/files/{id}` · `GET /v1/files/{id}/content` · `DELETE /v1/files/{id}`

**Vaults / Credentials** (`vlt_…`; secrets for MCP/env-vars, attached via `vault_ids`)
`…/v1/vaults`, `…/v1/vaults/{id}/credentials[/{cid}][/archive|/mcp_oauth_validate]`

**Memory Stores / Memories / Versions**, **Deployments** (`depl_…`, cron), **Skills** — see §4.4.

### 4.2 Key request bodies

**CreateAgent** — `model`/`system`/`tools`/`mcp_servers`/`skills` live **here**, not on the session:
```json
{ "name": "Coding Assistant", "model": "claude-opus-4-8",
  "system": "optional", "tools": [{"type": "agent_toolset_20260401"}],
  "mcp_servers": [{"type":"url","name":"github","url":"https://.../mcp/"}],
  "skills": [{"type":"anthropic","skill_id":"xlsx"}] }
```
Built-in toolset `agent_toolset_20260401` = bash/read/write/edit/glob/grep/web_fetch/web_search.

**CreateSession**:
```json
{ "agent": "agent_abc123",              // string | {type:"agent",id,version} | {type:"agent_with_overrides",...}
  "environment_id": "env_abc123",
  "title": "optional",
  "resources": [ {"type":"github_repository","url":"...","authorization_token":"ghp_...",
                  "mount_path":"/workspace/repo","checkout":{"type":"branch","name":"main"}} ],
  "vault_ids": ["vlt_abc123"] }
```

**SendEvents** — bare events, no `payload` wrapper:
```json
{ "events": [ { "type": "user.message", "content": [{"type":"text","text":"hi"}] } ] }
```

### 4.3 Event types (mode B, dotted)

**Outbound:** `user.message`, `user.interrupt` (bare `interrupt` also accepted),
`user.tool_confirmation` (`tool_use_id`, `result: allow|deny`),
`user.custom_tool_result` (`custom_tool_use_id`, `content`, `is_error`),
`user.define_outcome`, `system.message` (Opus 4.8 only).

**Inbound:** `session.status_running|idle|rescheduled|terminated`, `session.error`,
`session.deleted`; `agent.message|thinking|tool_use|tool_result|mcp_tool_use|mcp_tool_result|custom_tool_use|thread_context_compacted`;
`span.model_request_start|end` (end carries `model_usage`), `span.outcome_evaluation_*`;
multiagent `session.thread_created`, `session.thread_status_*`, `agent.thread_message_sent|received`;
stream-only live previews `event_start`/`event_delta` (delta type `content_delta`).
Sent events are **echoed** on the stream (first `processed_at:null`, then timestamped).

- `session.status_idle` carries `stop_reason` — `type:"requires_action"` means it's
  blocked on you (respond); otherwise the turn is done.
- **No SSE replay.** On reconnect, open the stream then fetch history
  (`GET …/events`) and de-dupe by event `id` (prefixed `sevt_`).

### 4.4 Errors, pagination, rate limits

**Errors** — standard Anthropic envelope, HTTP status + `error.type`:
```json
{ "type":"error", "error":{"type":"invalid_request_error","message":"..."},
  "request_id":"req_..." }
```
`400 invalid_request_error`, `401 authentication_error`, `403 permission_error`,
`404 not_found_error`, `409` (conflict → still `invalid_request_error`),
`413 request_too_large`, `429 rate_limit_error` (+`retry-after`),
`500 api_error`, `529 overloaded_error`.

**Pagination** — `page`/`next_page` cursor scheme (`limit`, `order`); response
`next_page` (null at end). Only `GET /v1/sessions` also returns `prev_page`
(backward pagination). Files/Batches use `after_id`/`before_id` + `has_more` instead.

**Rate limits** (per org): create ops 300 rpm, other ops 600 rpm, environments 60 rpm
/ 5 concurrent. `429` includes `retry-after`.

---

## 5. How a Remote Control session comes to exist (the host side)

You don't create RC sessions as a controller — the local CLI does, by acting as a
**bridge** (a private self-hosted environment worker). For context, the host flow is:

1. **Register bridge:** `POST /v1/environments/bridge`
   `{machine_name, directory, branch, git_repo_url, max_sessions, metadata:{worker_type:"claude_code"}}` → `{environment_id}`.
2. **Long-poll for work:** `GET /v1/environments/{env}/work/poll` → work items
   `{id, secret(base64url JSON {version:1, session_ingress_token}), data:{type:"session", id:"cse_..."}}`.
3. **Per-session worker:** `POST /v1/code/sessions/{id}/worker/register`
   (Bearer = `session_ingress_token`) → `{worker_epoch}`, then read **inbound** user
   events from `GET /v1/code/sessions/{id}/worker/events/stream`, `ack`/`heartbeat`
   the work item, and post `assistant`/tool events back via `POST …/{id}/events`.
4. **Session log mirror:** persisted to a `remoteIngressUrl`
   (`GET /v1/session_ingress/session/{id}` to read back), with `x-last-uuid` 409 reconciliation.

The **controller** (webpage / this library) does none of that — it only lists,
sends, and streams (§3). Bootstrap of Remote Control itself is a **local control-RPC**
(`this.request({subtype:"remote_control", enabled, name})`), not an HTTP call.

Relevant env vars: `CLAUDE_BRIDGE_SESSION_INGRESS_URL`, `CLAUDE_BRIDGE_REATTACH_SESSION`,
`CLAUDE_BRIDGE_OAUTH_TOKEN`, `CLAUDE_BRIDGE_BASE_URL`, `CLAUDE_CODE_REMOTE_SESSION_ID`,
`CLAUDE_CODE_REMOTE_SEND_KEEPALIVES`. Idle limit ~10 min offline before the session times out.

---

## 6. Practical recipe: steer a live session like the webpage

```python
from claude_rc import RemoteControlClient

rc = RemoteControlClient()                       # reads ~/.claude credentials
sid = rc.sessions()[0]["id"]                     # a live `claude remote-control` session

# ask + wait for the answer (sends, then streams from the pre-send cursor, stops on `result`)
for ev in rc.send_and_collect(sid, "summarize the diff", print_stream=True):
    pass

# or observe read-only
for ev in rc.stream_events(sid):
    if ev.role == "assistant":
        print(ev.text())
```

**Ordering rule:** pin the resume point *before* you send. Mode A is server-side
resumable, so connecting late with `from_sequence_num=<pre-send>` replays the gap
as one buffered batch rather than losing it (this is exactly what
`send_and_collect` does). Mode B has **no replay** (§4.3) — open the stream first
and reconcile against `GET …/events`, de-duping by event `id`. In both modes user
events queue server-side and process in order; `interrupt` jumps the queue.
