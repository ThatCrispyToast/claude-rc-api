# claude-rc-api

Unofficial Python client for the Claude Code Remote Control web service,
reverse-engineered from the Claude Code CLI and exercised against the live API.

It lets your own programs do what `claude.ai/code` and the Claude mobile app do:
list, observe, and steer Claude Code sessions running on a machine. It talks to
the same `/v1/code/sessions` endpoints and claude.ai OAuth token the web app
uses.

> ⚠️ **Unofficial and unsupported.** These are private endpoints
> (`api.anthropic.com`), reconstructed from the CLI binary. They can change or
> break at any time. Use your own account, at your own risk. Full protocol
> writeup in [`API_REFERENCE.md`](./API_REFERENCE.md).

---

## How it works

Remote Control relays, it doesn't run cloud compute. Your local
`claude remote-control` process runs Claude and holds an outbound connection to
Anthropic; the web app and mobile app act as a controller that posts messages
and reads the session's event stream. This library plays that controller role.

```
your script ──► POST /v1/code/sessions/{id}/events    (send a message)
            ◄── GET  /v1/code/sessions/{id}/events/stream  (SSE: read the reply)
   (claude.ai OAuth Bearer + x-organization-uuid, from ~/.claude)
```

It also ships a `ManagedAgentsClient` for the sibling public Sessions API
(`/v1/sessions`, `x-api-key`) that runs agents in Anthropic's cloud.

## Install

As a library / CLI, straight from the repo:

```bash
uv add "claude-rc-api @ git+https://github.com/ThatCrispyToast/claude-rc-api"   # into a uv project
pip install "git+https://github.com/ThatCrispyToast/claude-rc-api"              # into any venv
uvx --from "git+https://github.com/ThatCrispyToast/claude-rc-api[cli]" claude-rc list   # run the CLI, zero install
```

For development in a checkout, use [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra cli                 # runtime + pretty CLI
uv sync --extra cli --extra test    # + pytest
```

## Prerequisites

1. Log in to Claude Code with a claude.ai account (Pro/Max/Team/Enterprise):
   `claude` → `/login`. (API-key logins can't use Remote Control.)
2. Have a session to drive: run `claude remote-control` (or
   `claude --remote-control`) in some project. That registers a session you can
   then control from here.

Credentials load automatically: the OAuth token from `~/.claude/.credentials.json`
(on macOS, first from the login Keychain item `Claude Code-credentials`, as the
CLI does) and the `organizationUuid` from `~/.claude.json`. Override them with
`CLAUDE_RC_ACCESS_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_RC_ORG_UUID`,
and the Trusted Devices token with the CLI's `CLAUDE_TRUSTED_DEVICE_TOKEN`.

## CLI

```bash
claude-rc whoami                 # login / org / token status
claude-rc list                   # your active sessions (--all adds archived, --pinned filters)
claude-rc get   <cse_id>         # session details (JSON)
claude-rc events <cse_id>        # recent history
claude-rc watch <cse_id>         # stream live (read-only)
claude-rc info  <cse_id>         # summary, context, plan usage, git changes
claude-rc ask   <cse_id> "what are you stuck on?"  # side question
claude-rc send  <cse_id> "run the tests"   # send a message, print the reply
claude-rc repl  <cse_id>         # interactive chat
claude-rc rename <cse_id> "new title"
claude-rc pin   <cse_id>         # pin it in claude.ai/code's sidebar (unpin undoes it)
claude-rc web                    # browser control panel (all sessions)
```

`ask` is claude.ai's "btw": the worker answers from its conversation without
interrupting a running turn or adding to the transcript (it costs a model call).

## Web UI

A dependency-free browser control panel for your Remote Control sessions: list
them (pins first, with each one's effort, context use, and status summary; tick
*archived* to include those), watch the live event stream, send messages, ask
side questions, allow or deny tool permissions, and steer (interrupt / model /
permission mode / effort / pin / archive and unarchive).

```bash
claude-rc web                    # serves http://127.0.0.1:8765 and opens it
claude-rc web --port 9000 --no-open
```

It's a standard-library `http.server` (no new dependencies) that proxies the
`RemoteControlClient`. It speaks to the private API with *your* OAuth token and
does no auth of its own, so it binds to `127.0.0.1` by default. Pass
`--host 0.0.0.0` only if you accept that anyone who can reach it can steer your
sessions. You can also run it programmatically:

```python
from claude_rc import serve_webui
serve_webui(host="127.0.0.1", port=8765, open_browser=True)
```

## Library

```python
from claude_rc import RemoteControlClient

rc = RemoteControlClient()                 # reads ~/.claude, refreshes token as needed

# discover sessions (a page is the 20 newest, archived included, unless filtered)
for s in rc.sessions(statuses="active"):
    meta = s.get("external_metadata") or {}
    print(s["id"], s["worker_status"], s["title"], meta.get("effort_level"))

sid = rc.sessions(statuses="active")[0]["id"]

# ask and wait for the answer (sends, streams from the pre-send cursor, stops on `result`)
for ev in rc.send_and_collect(sid, "summarize the current diff", print_stream=True):
    pass

# observe read-only
for ev in rc.stream_events(sid):
    if ev.role == "assistant":
        print(ev.text())
    if ev.type == "result":
        break

# steer
rc.interrupt(sid)
rc.set_model(sid, "claude-opus-5-5")
rc.set_permission_mode(sid, "acceptEdits")
rc.set_effort(sid, "high", wait=6.0)        # low|medium|high|xhigh|max, or None = auto
rc.rename_session(sid, "release prep")
rc.pin_session(sid)                         # claude.ai/code's Pinned group; unpin_session undoes it
rc.sessions(tags=PINNED_TAG)                # the pins (from claude_rc import PINNED_TAG)
rc.archive_session(sid); rc.unarchive_session(sid)

# follow the session list instead of polling it
page = rc.list_sessions(statuses="active")
for kind, s in rc.watch_sessions(resume_token=page["resume_token"]):
    print(kind, s["id"], s["worker_status"], s["title"])   # kind: added | changed

# answer the worker's prompts
for ev in rc.stream_events(sid):
    if ev.is_blocking_control and ev.control_subtype == "can_use_tool":
        req = ev.payload["request"]
        rc.respond_permission(sid, ev.request_id, allow=True,
                              tool_input=req["input"], tool_use_id=req.get("tool_use_id"))
        break
    if ev.settles_prompt:                   # someone else answered ev.request_id, or the worker withdrew it
        break

# ask the worker — each waits for its verdict (ControlRejected / ControlTimeout)
rc.side_question(sid, "what are you stuck on?")   # "btw": no interruption, not in the transcript
rc.get_context_usage(sid)                   # {totalTokens, maxTokens, percentage, categories…}
rc.get_usage(sid)                           # plan limits: rate_limits.limits[{kind, percent, resets_at}]
rc.get_workspace_diff(sid)                  # {stats: {filesCount, linesAdded, linesRemoved}, …} | None
rc.stop_task(sid, "b5rkopq8x")              # task_id from a system/task_started event

# withdraw a message before the worker starts on it
import uuid
mid = str(uuid.uuid4())
rc.send_message(sid, "actually, run the linter too", message_uuid=mid)
rc.cancel_queued_message(sid, mid)          # True while it was still queued

# any other control subtype (see API_REFERENCE.md §3.2)
rc.request_control(sid, "mcp_status")
```

The session object already carries the cheap answers, no round trip needed:
`external_metadata` has `effort_level`, `context_usage` (`used_tokens` /
`max_tokens`), `post_turn_summary` (a one-line `status_detail`, plus
`needs_action` when it waits on you) and the worker's `container_cc_version`,
and `tags` holds `ui:pinned` for a pinned session.

Cloud (Managed Agents) mode:

```python
from claude_rc import ManagedAgentsClient
ma = ManagedAgentsClient(api_key="sk-ant-...")
agent = ma.create_agent(name="A", model="claude-opus-5-5",
                        tools=[{"type": "agent_toolset_20260401"}])
env = ma.create_environment(name="e", config={"type": "cloud",
                            "networking": {"type": "unrestricted"}})
sess = ma.create_session(agent=agent["id"], environment_id=env["id"])
ma.send_message(sess["id"], "hello")
for ev in ma.stream_events(sess["id"]):
    if ev.type == "agent.message": print(ev.text())
    if ev.type == "session.status_idle": break
```

See [`examples/`](./examples).

## Events

`Event` normalizes both wire formats. For Remote Control the important payload
types are `user`, `assistant`, `result` (turn complete), `control_request` /
`control_response` (steering, prompts, and their answers), and `system` (`init`,
`status`, task events). Helpers:

```python
ev.type          # payload type
ev.role          # "user" | "assistant" | None
ev.text()        # concatenated text blocks (handles str or block-list content)
ev.tool_uses()   # tool_use content blocks
ev.sequence_num  # RC ordering/resume cursor (int)
ev.source        # "client" (a controller) | "worker" (Claude Code)
ev.is_turn_end   # a `result` (RC) or `session.status_idle` (managed agents)
ev.is_blocking_control  # a worker prompt waiting on you (never a controller's own request)
ev.settles_prompt  # that prompt is over: a controller answered it, or the worker withdrew it
ev.request_id    # the id a control_request carries / a control_response answers / a cancel withdraws
ev.worker_state  # system/init|status → {model, effort, permission_mode, claude_code_version}
ev.attestation   # "UNSPECIFIED" | "VERIFIED" | "ABSENT" … (Trusted Devices)
```

## Safety

- `list`/`get`/`events`/`watch` and the `RemoteControlClient` read methods
  (`watch_sessions` included) are read-only. `send`/`repl`/`send_message`/
  `interrupt` and the other steering calls inject into a live session, so only
  use them on sessions you own. `rename`, `pin` and `archive` change what every
  controller's list shows. The
  worker queries (`ask`, `info`, `side_question`, `get_*`) leave a
  `control_request` / `control_response` pair in the event log, as claude.ai's
  own attach does; a side question also costs a model call.
- Orgs that require Trusted Devices refuse an unenrolled machine with a 403
  `untrusted_device`. The client then enrolls it as the CLI does
  (`enroll_trusted_device`, which emails the account owner), saves the token
  as `trustedDeviceToken` next to the login that enrolled, and retries, with
  the CLI's guards against enrolling in a loop. Workers can still drop
  controller events that carry no device signature; this library doesn't
  sign. `ev.attestation` shows what the server recorded, and
  API_REFERENCE.md §3.4 has the details.
- Token refresh writes the new tokens back where they came from (the
  credentials file or the macOS Keychain item) the way the CLI's own refresh
  does: only over the login it refreshed, keeping the rest of the document.
  Neither that nor an enrollment takes the CLI's lock files, so a write
  racing one of the CLI's can lose one side. `persist_refresh=False` keeps
  refreshes in memory and turns lazy enrollment off, but the refresh token a
  refresh spends is the CLI's too, so the CLI must sign in again.
- This project never prints or transmits your tokens.

## Used by

[`claude-remote-bridge`](https://github.com/ThatCrispyToast/g2-claude-remote)
(the `server/` half of the Claude Remote glasses app) depends on this package
straight from git and wraps `RemoteControlClient` in a JSON + SSE HTTP bridge. It
imports `client`, `credentials`, and `events` (prompt answers, side questions,
the session metadata) and resolves this repo's default branch on each fresh `uv`
install — so keep those three backwards-compatible.

## Project layout

```
claude_rc/
  credentials.py   # OAuth load + refresh (file or macOS Keychain), org uuid
  client.py        # RemoteControlClient (mode A) + ManagedAgentsClient (mode B)
  events.py        # Event model + builders for both wire formats
  sse.py           # dependency-free SSE parser
  cli.py           # `claude-rc` command line
  webui.py         # `claude-rc web` — stdlib http.server control panel
  static/          # the web UI single-page app
API_REFERENCE.md   # full reverse-engineered protocol reference
examples/          # runnable examples
tests/             # offline unit tests (pytest)
```

## Tests

```bash
uv run --extra test pytest -q
```

All tests run offline (no network, no credentials required).
