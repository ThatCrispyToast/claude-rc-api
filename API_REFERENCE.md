# Claude Remote Control API — Reverse-Engineered Reference

> **Unofficial.** Reconstructed from the Claude Code CLI (a Bun single-file
> executable; last mined at 2.1.322) and the API docs it embeds. These are private
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

On **macOS** the CLI keeps the same JSON document as the secret of a
login-Keychain generic password: service **`Claude Code-credentials`**,
account `$USER` (or `claude-code-user` when the name has characters outside
`[A-Za-z0-9._-]`). It reads the Keychain first and falls back to the file, and
when a Keychain write succeeds where no item existed, it deletes the file
(2.1.312). Its calls, which this library mirrors:
```
security find-generic-password -a <account> -w -s "Claude Code-credentials"   # exit 44 = no item
security -i <<< 'add-generic-password -U -a "<account>" -s "<service>" -X "<hex of the JSON>"'
```
The hex goes over `security -i`'s stdin so `ps` can't see it, except when the
command is longer than 4032 characters (an `mcpOAuth` block gets there): then
the CLI passes the same arguments on argv. An item that isn't valid JSON reads
as no item. The service name has two variants this library doesn't follow
(it reads only the default item and file): a config-dir suffix
`-<first 8 hex of sha256(dir)>`, taken from `CLAUDE_SECURESTORAGE_CONFIG_DIR`
when that is set at all (set but empty: no suffix) and otherwise from
`CLAUDE_CONFIG_DIR`, and `CLAUDE_CODE_CUSTOM_OAUTH_URL`, which makes it
`Claude Code-custom-oauth-credentials`. `load_credentials()` reads the Keychain
first on macOS (unless given an explicit path), and refreshes write back to
the same item.

**Saving a refresh** (2.1.312, both stores): the CLI takes
`~/.claude/.oauth_refresh.lock`, gives up if the store can't be read (no
token spent), adopts tokens another process already rotated, and saves with a
compare-and-swap: only while the stored `refreshToken` is still the one it
posted (or `""`). A store that a sign-in or sign-out has changed is left
alone. It keeps the block's other fields (`subscriptionType`, `rateLimitTier`,
…), and every store write holds `~/.claude/.storage-write.lock`.
`refreshTokenExpiresAt` is when the login as a whole ends (the CLI warns as it
nears), and a refresh never moves it later (2.1.322): the stored time stays, or
drops to the response's `refresh_token_expires_in` when that is sooner. A
stored value that isn't an epoch-ms time between 2025 and ten years ahead is
removed. This
library follows the same rules without the two locks, and a little stricter:
a Keychain it can't read stops the refresh (the CLI may still refresh from
its 30-second cache). `RemoteControlClient` adopts rotated tokens, and
`refresh_credentials()` checks the store before posting and saves after. If
that save fails, it returns the new tokens anyway (they are the only valid
pair), logs a warning, and sets `stale_refresh_token` to the spent token the
store still holds. Until a later refresh saves over it, the client doesn't
reload that stale pair, or the `""` the CLI clears it to.

**Organization UUID** (the `x-organization-uuid` header) lives in
**`~/.claude.json`** → `oauthAccount.organizationUuid` (a top-level
`organizationUuid` in `.credentials.json` is used as a fallback). `/v1/code/*`
answers without the header (list, get and events checked live 2026-11-10; the
CLI's get / update / archive calls send it only on the `/v1/sessions` compat
path), so a missing UUID is not fatal.

**Request headers** (builder `DC(token)` + `Gnd`/`l$g`):
```
Authorization:              Bearer <accessToken>
Content-Type:               application/json
anthropic-version:          2023-06-01                 # the ONLY version string ever sent
anthropic-client-platform:  web_claude_ai              # LOAD-BEARING on event ingest — see §2.3
x-organization-uuid:        <organizationUuid>         # optional on /v1/code/*
anthropic-beta:             ccr-byoc-2025-07-29         # only on the CLI's /v1/sessions compat path; /v1/code/* needs none
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
Response is standard OAuth (`access_token`, `refresh_token`, `expires_in`, and
`refresh_token_expires_in` when the login has an end). Refresh when
`expiresAt <= now`. 2.1.322's own login refresh also sends `scope`
(space-separated) in that body. `client_id` overridable via `CLAUDE_CODE_OAUTH_CLIENT_ID`;
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
| `ccr-byoc-2025-07-29` | Mode A's `/v1/sessions` compat path (not `/v1/code/*`) |
| `managed-agents-2026-04-01` | Mode B endpoints |
| `oauth-2025-04-20` | OAuth token refresh |
| `files-api-2025-04-14` | `/v1/files` (both modes) |
| `oidc-federation-2026-04-01` | enterprise WIF/OIDC token exchange |

`anthropic-client-platform` — **since Claude Code ~2.1.220 this header decides
whether a sent message is treated as human.** CCR ingress stamps the header
value into each ingested event as `client_platform` (the event schema marks it
"Injected server-side by CCR ingress from the request header"; the sibling
`inbound_origin` is likewise server-asserted and not client-settable). The
worker classifies a user message as a *human controller* message only when the
stamped value is one of `ios`, `android`, `web_claude_ai`, `desktop_app`
(exact set from the 2.1.234 binary). Any other value — including
`claude_code_remote`, the CLI's own `CLAUDE_CODE_ENTRYPOINT` telemetry enum
(`claude_code_cli`, `claude_code_vscode`, `claude_code_sdk`, …) that this
library sent before 0.2.1 — demotes the message to **peer origin**: the worker
logs `[bridge] demoting unwrapped inbound message to peer origin`, frames it as
"Another Claude session sent a message" with `isMeta`, and its peer gate may
park it without ever starting a turn (the "message appears but Claude never
responds" failure on ≥2.1.220 workers; 2.1.219 predates the gate and ran every
inbound message). Send `web_claude_ai`.

---

## 3. Remote Control API (mode A — the controller surface)

Base: `https://api.anthropic.com`. All calls use the Mode-A headers from §2.1.
`{id}` is a `cse_…` session id. Everything marked *verified* was exercised live
against Claude Code **2.1.301** workers on 2026-10-16; the rest is read from the
binary. Rechecked against **2.1.322** on 2026-11-10: the worker's dispatch table
(§3.2) is unchanged, and what is new is dated where it appears.

### 3.1 Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/code/sessions` | List your sessions, newest first — `?limit`, `?cursor=<next_cursor>`, and the filters below |
| `GET` | `/v1/code/sessions/watch` | SSE stream of changes to that list (below) |
| `GET` | `/v1/code/sessions/{id}` | Get one — **wrapped**: `{"response_shape": {…session…}}` |
| `PUT` | `/v1/code/sessions/{id}` | Update `{"title"}` or `{"add_tags", "remove_tags"}` → `{"session": {…}}` (verified; the CLI's own title/colour sync) |
| `POST` | `/v1/code/sessions/{id}/archive` | Archive/end (empty body `{}`; **200 or 409** = success) |
| `POST` | `/v1/code/sessions/{id}/unarchive` | Back to `active` → `{"session": {…}}`; **409** `invalid_status_transition` when it wasn't archived (verified 2026-11-10). The CLI expects a 403 `untrusted_device` / `session_stale_relogin` here from orgs that want elevated auth |
| `POST` | `/v1/code/sessions/{id}/mark_read` | Clear `unread` → `{"unread": false}`; optional `{event_id}` (an event's UUID, what the CLI sends) names the last one read. A `sequence_num` field is ignored |
| `POST` | `/v1/code/sessions/{id}/client/presence` | Announce/clear a watching client |

There is **no DELETE** for code sessions — archive only. Session **creation** is done
by the local CLI, not the controller (see §5). A list page is
`{"data": [...], "next_cursor": …, "resume_token": …}` with the sessions bare;
only the single-session `GET` wraps its object in `response_shape`, with or
without `anthropic-beta` (checked live 2026-10-28). `get_session()` and
`update_session()` return the session itself.

**List filters** (verified 2026-11-10). A page holds `limit` sessions: 20 by
default, 200 at most, archived ones included. Repeat a parameter to give several
values (a comma-joined value is read as one):

| Parameter | Keeps |
|---|---|
| `statuses` | sessions in that `status` (`active`, `archived`) |
| `tags` | sessions carrying **any** of the tags |
| `exclude_tags` | sessions without the tag |

Unknown parameters are ignored, so a misspelt filter returns the unfiltered list.

**Watching the list** (verified 2026-11-10; the CLI's bridge uses it to learn
when to poll for work). `GET /v1/code/sessions/watch` with
`Accept: text/event-stream` and `Last-Event-ID: <resume_token>`, the token a
list page returned (the CLI mints one with `?limit=1`, and adds
`?include_trigger_sessions=true&exclude_tags=-`). Without a token it answers
`400 resume_token required — call ListSessions first`. Frames:
- `sync` — the stream is caught up; its `id:` is a newer resume token. One
  arrives at connect, and the CLI redials a stream that goes 6–9 minutes
  without another.
- `added` / `changed` — data is the whole session object. `added` was the first
  frame for a session on a watch, whether the session was new or had just
  changed for the first time since the token; later changes came as `changed`.
- `:keepalive` comments about every 13 s. The CLI redials a stream silent for 45 s.

As the CLI reads the refusals: `410` means the token is too old to resume from
(list again), `404` that the account isn't offered the stream, and `429`
carries `retry-after`.

**Session object** (verified):
```json
{
  "id": "cse_019B9e41WcXsxvZmtvJK9SEB",
  "title": "Add audio transcript functionality",
  "status": "active",                 // active | archived
  "status_bucket": "working",         // working | completed | blocked (lags: stays "blocked" after the block clears)
  "worker_status": "running",         // idle | running | requires_action
  "connection_status": "connected",   // connected | connecting | disconnected
  "environment_kind": "bridge",       // Remote Control = "bridge"
  "config": { "model": "claude-opus-5-5", "origin": "claude_code_cli", "mcp_connector_ids": [], … },
  "created_at": "2026-10-16T15:25:49.307782Z",
  "last_event_at": "2026-10-16T15:53:22.309103Z",
  "updated_at": "2026-10-16T15:53:22.309103Z",
  "environment_id": "",               // empty for a bridge session
  "user_message_count": "0",
  "tags": ["remote-control-repl", "config:auto-create-pr:off", "ui:pinned"],  // "ui:pinned" = pinned in claude.ai/code
  "unread": true,
  "external_metadata": {
    "container_cc_version": "2.1.301",          // the worker's Claude Code version
    "model": "claude-opus-5-5",
    "effort_level": "max",                      // the worker's CURRENT effort
    "context_usage": { "used_tokens": 44935, "max_tokens": 1000000, "after_boundary": "<uuid>" },
    "post_turn_summary": {                      // a server-side one-liner per turn
      "status_category": "completed",           // completed | need_input | …
      "status_detail": "rag committed and pushed; answers via OpenRouter Haiku",
      "needs_action": "",                       // what it waits on you for, when it does
      "recent_action": "…"
    },
    "rate_limit_info": { "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1792180800, "isUsingOverage": false },
    "usage": { "input_tokens": 2466, "output_tokens": 145184, "cache_read_tokens": …, "cache_write_tokens": … },
    "flag_settings": { "effortLevel": "xhigh", "ultracode": false },  // server fold of every apply_flag_settings SENT — rejected ones too
    "current_branches": { "owner/repo": "main" }, "worktree_state": { … },
    "pending_action": { … }                     // while worker_status == requires_action
  },
  "participants": [], "relations": []
}
```
The single-session `GET` adds `security_tier` (`standard` | `elevated`),
`session_url` (its claude.ai page), `metadata`, `requires_action_details_list`
and `client_presence`: who is watching, as
`[{client_id, last_pulse_at, platform}]` (verified 2026-11-10).

`requires_action` on `worker_status` means the agent is blocked on you (a
permission prompt or dialog); details live in `external_metadata.pending_action`.
The session has **no permission-mode field** — read it from the `system/init` /
`system/status` events (§3.3). Prefer `effort_level` over `flag_settings` (the
latter keeps values the worker refused).

**`POST …/client/presence`** — body `{client_id, connected_at}` (ISO-8601) to
announce, or `{client_id, clear:true}` to leave. The web client pulses this, and
each pulse shows in the session's `client_presence`.

**Pinned sessions** carry the tag **`ui:pinned`** (verified 2026-11-10: adding
it through `PUT …/{id}` with `{"add_tags": ["ui:pinned"]}` moved a session into
claude.ai/code's *Pinned* group, and `remove_tags` took it back out). So
`?tags=ui:pinned` lists the pins, and `pin_session()` / `unpin_session()` set
them. Archived sessions keep the tag.

claude.ai also keeps a per-user settings document this library can't reach, since
it **refuses OAuth tokens** (`403 permission_error`, `error_code:
oauth_token_not_accepted`, on both `api.anthropic.com` and `claude.ai`, checked
2026-10-30): `GET` / `PUT /api/claude_code/organizations/{org}/user_settings`,
`{userId, version, lastModified, checksum, content: {entries}}`, written one key
at a time with `If-Match`. Its key `ccd/dframe-starred-code` held a `starredIds`
list of `session_…` ids that matched the pins on that date; other keys seen were
`ccd/dframe-starred-cowork-remote` and `ccd/dframe-store` (sidebar state). The
Claude Code CLI has no client for it (`/api/claude_code/settings` is org-managed
policy, not this).

### 3.2 Sending events (steering)

`POST /v1/code/sessions/{id}/events`
```json
{ "session_id": "cse_...",
  "events": [ { "payload": { /* stream-json message, gets a uuid if missing */ } } ] }
```
Each event is wrapped in `payload`, and the client adds a `uuid` if the payload
lacks one. The ack (verified) is
`{"results": [{"event_id", "sequence_num", "duplicate"}], "attestation_feedback": null}`
— `sequence_num` is the event's place in the log, a ready cursor for §3.3.
The CLI's own controller reads two refusals here: `409` with
`error.type: session_not_active`, and `413` (or a `400` naming
`remote_tool_result_too_large`) for an event too big to store.
There is also a separate outbound channel `POST …/{id}/teleport-events`
(screen-mirroring) — not needed for steering.

**Send a user message** — payload is Claude Code stream-json, **not** `user.message`:
```json
{ "type": "user", "uuid": "<optional: your handle for cancel_async_message>",
  "message": { "role": "user",
    "content": [ { "type": "text", "text": "run the tests" } ] } }
```
`content` accepts the full Messages-API block union: `text`,
`image` (`{source:{type:"base64",media_type,data}}`), `tool_result`, plus file
attachments. A message sent while a turn runs waits in the worker's queue, and
is usually folded into that turn rather than starting the next (§3.3, `result`).

**Steer and query via `control_request`** — payload
`{type:"control_request", request_id, request:{subtype, ...}}`. The POST only
queues it: the worker's verdict arrives later as a `control_response`
(`response: {subtype: success|error, request_id, response?, error?, error_code?}`)
in the event log, with `source: "worker"`. An error may name its kind in
`error_code` (`workspace_diff_pending`, `invalid_mode`, `internal_error` …),
which `ControlRejected.error_code` carries. **The remote-control (REPL)
worker's dispatch table** (2.1.301, the same subtypes in 2.1.322; each verified
live unless noted):

| `request.subtype` | fields | verdict `response` / notes |
|---|---|---|
| `initialize` | `supportedDialogKinds?` | `{claude_code_version, current_model, current_permission_mode, commands, agents, models, pid, inbound_read_receipts, …}`, with `pending_permission_requests` / `pending_user_dialog_requests` beside it. claude.ai sends it on every attach (often twice) and the worker answers each with a fresh `system/init`. |
| `interrupt` | — | stops the running turn (jumps the queue) |
| `set_model` | `model` (`null`/`"default"` = default) | may post `system/informational` notices, and a success may carry `{model}` (2.1.322); `only_if_model_is_current` is refused over RC |
| `set_permission_mode` | `mode` (`default` \| `plan` \| `acceptEdits` \| `bypassPermissions`) | |
| `apply_flag_settings` | `settings: {effortLevel?, ultracode?}` — nothing else over RC | `effortLevel` ∈ `low` `medium` `high` `xhigh` `max`, `null` = auto; anything else → `"unrecognized effortLevel"`. **Session-scoped**: re-emits `system/init` with the new `effort`, persists nothing. This is how the CLI's `/effort` reaches a worker — there is no `set_effort` subtype. ⚠️ Refused by RC workers through at least 2.1.212 (`"REPL bridge does not handle…"`); `RemoteControlClient.set_effort()` falls back to the `/effort` slash command there. |
| `set_max_thinking_tokens` | `max_thinking_tokens` (int \| null), `thinking_display?` | unverified |
| `side_question` | `question`, `history?: [{question, response, fallback_notice?}]` | `{response: string \| null, synthetic: bool}` — claude.ai's "btw": answered from the conversation without interrupting the turn or entering the transcript (one model call; ~2–3 s on Haiku). `null` = nothing to answer from (empty conversation); `synthetic: true` carries a canned "(No answer available…)". Progress posts as `system/control_request_progress`; 11-minute deadline; `control_cancel_request` ends it with error `"Side question cancelled"`. |
| `cancel_async_message` | `message_uuid` | `{cancelled: bool}` — withdraws a controller's queued user message (its payload `uuid`); `false` once a turn picked it up |
| `stop_task` | `task_id` | `{}` — the task posts `task_updated` (`killed`) → `task_notification` (`stopped`); unknown ids are acked too |
| `background_tasks` | `tool_use_id?` | backgrounds the running foreground shell task (`{backgrounded}`), or all of them — unverified |
| `get_context_usage` | `detail?: summary \| full` | `{totalTokens, maxTokens, percentage, model, autoCompactThreshold, isAutoCompactEnabled, categories:[{name, tokens, kind}], gridRows, messageBreakdown, …}` |
| `get_usage` | `skip_behaviors?` | `{subscription_type, rate_limits:{limits:[{kind: session \| weekly_all \| weekly_scoped, percent, resets_at, severity, scope}], five_hour, seven_day, …}, session:{total_cost_usd, total_lines_added, …}}` |
| `get_workspace_diff` | — | `{diff: {stats:{filesCount, linesAdded, linesRemoved}, perFileStats, hunks, restricted, skippedLarge, source} \| null}` — `null` outside git; an 8 s budget, then error_code `workspace_diff_pending` |
| `mcp_status` | — | `{mcpServers: [{name, status}]}` |
| `rename_session` | `title` | renames the worker's *local* session only — the title controllers list is `PUT /v1/code/sessions/{id}` (§3.1) |
| `set_color` | `color` | unverified |
| `file_suggestions` / `read_file` | `query` / `path, max_bytes?, encoding?, anchor?` | `{suggestions:[{path}]}` / file content + `absPath` — unverified |
| `mcp_authenticate`, `mcp_oauth_callback_url`, `mcp_reconnect`, `mcp_set_servers` | | MCP management; claude.ai's `mcp_set_servers` (server-injected MCP) is refused on RC sessions |
| `fork_conversation` | | `{forked: false, reason: "requires_remote_control_server"}` on a plain RC worker |

Anything else gets `"REPL bridge does not handle control_request subtype: …"`:
claude.ai's `rewind_conversation` (`target_message_uuid`,
`last_seen_user_message_uuid`) is one, seen refused by a 2.1.318 worker.
claude.ai also sends `end_session` with `reason: "archived"`; the logs hold no
verdict for it.
A process that is shutting down refuses everything but the read-only queries.
To withdraw an in-flight request, send `{type:"control_cancel_request", request_id}`.

**Answering the worker's prompts.** A worker blocks its turn with a
`control_request` of its own (`source: "worker"`): `can_use_tool` (a tool
permission) or `request_user_dialog`. Answer with a `control_response`:
```json
{ "type": "control_response",
  "response": { "subtype": "success", "request_id": "<the prompt's>", "tool_use_id": "<optional>",
                "response": { "behavior": "allow", "updatedInput": { /* optional */ } } } }
```
or `{"behavior": "deny", "message": "…"}`. `updatedInput` is optional in the
2.1.301 schema (older workers expect it — echo the request's `input`).
**AskUserQuestion arrives as `can_use_tool`**, not a dialog: answer it with an
allow whose `updatedInput` is the tool's input plus
`answers: {<question text>: <label | [labels]>}` (an empty map dismisses it; the
tool crashes if `questions` is missing). claude.ai's own allow carries
`toolUseID` and `tool_name` inside that decision, next to `updatedInput`.

A prompt ends one of three ways, and a controller should watch for each: any
controller's answer echoes back as a `control_response` (`source: "client"`);
the worker withdraws it with a `control_cancel_request` naming the prompt's
`request_id` (`source: "worker"`: it was answered at the terminal, or the turn
moved on); or the turn's `result` arrives. `Event.settles_prompt` is true for
the first two.

**Slash commands work as user messages** (confirmed live, 2026-07-17): a `user`
event whose text is e.g. `/context` is executed by the remote-control worker
as a **local command** — zero cost, `num_turns: 0`, no API call; the transcript
gains the synthetic `<command-name>` user events plus a synthetic assistant echo
of the command output. Semantics are exactly "typed at that terminal" — so
`/effort <level>` **persists that machine's default** for the model, unlike the
`apply_flag_settings` control.

### 3.3 Receiving events

**Envelope** (both transports, verified):
`{sequence_num, event_id, event_type, source, created_at, device_attestation_status, sent_by_account_id, payload}`.
`source` is `client` (a controller — claude.ai, the apps, this library) or
`worker` (Claude Code). A controller's events also carry the delivery timestamps
`received_at` / `processing_at` / `processed_at`. `sequence_num` arrives as a
**string** — coerce to int.

**Stream (SSE):** `GET /v1/code/sessions/{id}/events/stream?from_sequence_num=N`
- Replays every persisted event after `N` (**exclusive**; `0` replays the whole
  backlog), then follows live. Reconnect with **both** `?from_sequence_num=<last>`
  **and** a `Last-Event-ID: <last>` header and de-dupe by `sequence_num`.
- `&control_only=1` narrows it to the control protocol: `control_request` and
  `control_response` events only (verified 2026-11-10).
- Frame kinds (`event:` line):
  - `client_event` — a persisted event; `id: <sequence_num>`, data = the envelope.
  - `ephemeral_event` — never persisted, no sequence number: `stream_event`
    deltas and `system/thinking_tokens`.
  - `session_update` — `{connection_status, …}` changes.
  - `delivery_update` — `{event_id, status, timestamp}` for a controller's event:
    `DELIVERY_STATUS_RECEIVED` → `…_PROCESSING` (it left the queue for a turn)
    → `…_PROCESSED` (the turn ended).
  - `catch_up_truncated` — a history gap.
  - `security_update` — `{security_tier}`, e.g. `elevated` (2.1.322, from the
    binary).
- Comment lines (`:keepalive`) are heartbeats: one at connect, then about every
  15 s, on an archived session's stream too. The CLI redials a stream that has
  been silent for 45 s, and so does `stream_events()`.

**Poll (history):** `GET /v1/code/sessions/{id}/events?limit=N&sort_order=desc&cursor=C`
- `sort_order=asc` starts at the session's **first** event, `desc` at its newest
  (the CLI fetches `desc`). `cursor` is a sequence number: `asc` returns the
  events after it, `desc` the events before it. **`from_sequence_num` is
  ignored here** (stream only).
- Returns `{data:[envelope, ...], next_cursor, resume_cursor}`.

**stream-json payload `type` values** (the RC event vocabulary):

| `payload.type` | meaning |
|---|---|
| `system` / `init` | the worker's live settings: `model`, `effort`, `permissionMode`, `claude_code_version`, `fast_mode_state`, `capabilities`, `terminal_slash_commands`, `cwd`, `tools[]`… Re-posted on every model / mode / effort change and every controller `initialize`, so the newest one is current. |
| `system` / `status` | `permissionMode` + `status` (e.g. `compacting`, or `null`) |
| `system` / `compact_boundary` | context was compacted |
| `system` / `task_started`, `task_updated`, `task_notification` | a shell/agent task's lifecycle: `task_id`, `task_type` (`local_bash` …), `description`, `is_backgrounded`, `tool_use_id`; the notification carries `status` (`completed` \| `stopped` …) and `summary` |
| `system` / `background_tasks_changed` | `tasks: [{task_id, task_type, description, run_id}]` — the running background set |
| `system` / `control_request_progress` | a long control (side question) working: `request_id`, `status` |
| `system` / `informational` | a notice (`content`, `level`) |
| `system` / `vcs_state_changed` | the worker's repo moved: `cwd`, `kind` (`push` …) |
| `system` / `worker_shutting_down` | the Claude Code process is exiting (`reason: host_exit`); a `result` followed it in the logs |
| `user` | a user turn — `{message:{role:"user", content:[...]}}` (sends, echoes, tool results) |
| `assistant` | assistant turn — `{message:{role:"assistant", content:[text\|thinking\|tool_use...]}}` |
| `result` | **turn complete** — `{subtype: success\|error_max_turns\|error_during_execution, is_error, num_turns, duration_ms, total_cost_usd, usage, stop_reason, terminal_reason, user_message_uuid}`. `user_message_uuid`, when present, is the `uuid` of the user message that started the turn; a message sent mid-turn usually joins the running turn instead of getting a `result` of its own |
| `stream_event` | partial streaming (ephemeral) |
| `control_request` / `control_response` / `control_cancel_request` | the control protocol (§3.2), from either side — `source` says whose |
| `conversation_reset` | `/clear` and its kin (`trigger: clear`, `new_conversation_id`): the events before it are no longer the conversation |
| `rate_limit_event`, `tool_progress`, `tool_use_summary`, `active_goal`, `auth_status`, `env_manager_log` | auxiliary UI/telemetry events |

A turn ends on a **`result`** event (not `session.status_idle` — that's mode B).

### 3.4 Device attestation (Trusted Devices)

Every envelope carries `device_attestation_status`: `UNSPECIFIED`, `ABSENT`,
`VERIFIED`, `VERIFIED_BY_GATE`, `VERIFIED_KEYLESS_DEVICE`, `INVALID`, `UNCHECKED`,
`SERVICE_VOUCHED`, `SERVER_AUTHORED`, `SERVER_REPLAYED`, `TRANSITIVELY_VOUCHED`
(prefixed `DEVICE_ATTESTATION_STATUS_` on the wire). 2.1.301 workers filter
inbound controller events on it, by a policy the server sends
(`{accept_level, accept_statuses}`, default level `VERIFIED`). Without an
enforcing policy they accept and log unverified events — every event seen on
this project's personal Max account (October 2026) was `UNSPECIFIED`, claude.ai's
own included. Under
enforcement, a worker **drops** unverified user messages, prompt answers and the
mutating controls (`set_model`, `set_permission_mode`, `interrupt`, `stop_task`,
`background_tasks`, `cancel_async_message`, `set_max_thinking_tokens`,
`rename_session`, `set_color`, `apply_flag_settings`, `side_question`, the MCP
controls), and posts an assistant notice: *"Remote Control ignored a message that
arrived without a valid device signature (attestation: ABSENT). Re-pair the
sending device in Trusted Devices."* This library does not sign (it does send
`X-Trusted-Device-Token` when the credentials store has one); the send ack's
`attestation_feedback` is where the server would say more. A signing client
(2.1.322, from the binary) posts each event as
`{"payload": {…}, "device_attestation": {…}}`.

**Enrollment** (2.1.312): `POST /api/auth/trusted_devices` on
`api.anthropic.com` with the OAuth bearer and
`{"display_name": "Claude Code on <host> · <platform>"}` returns
`{device_token, device_id}`, and emails the account owner. The token goes in
the credentials store's top-level `trustedDeviceToken` (the
`CLAUDE_TRUSTED_DEVICE_TOKEN` env var overrides it and stops enrollment, for
the CLI and this library alike) and rides requests as
`X-Trusted-Device-Token`. An org that requires Trusted Devices refuses an
unenrolled caller with
`403 {"error": {"resource": "untrusted_device", "sub_reason": …}}`. The CLI
enrolls at sign-in, and lazily on such a refusal from its session client:
only when the token itself was refused (no `sub_reason`, or
`bound_unattested`; `served_call_unsigned` wants a device signature instead),
at most once every 5 minutes per process, and not while the refused token is
one a refusal enrolled less than a day ago (it stamps
`trustedDeviceEnrolledAfterRefusalAtMs`). A token the store gained meanwhile
is retried first, and enrollment uses the login the store holds; a
server-side switch can also turn enrollment off. `RemoteControlClient` does
the same lazy enrollment, as `claude-rc-api on <host> · <platform>`, when its
credentials came from a store it may write to, except that it can't see that
switch. It first retries as the store's login if a sign-in replaced its own,
and saves the token only next to the login that enrolled.

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
{ "name": "Coding Assistant", "model": "claude-opus-5-5",
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
