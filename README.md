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

Credentials load automatically from `~/.claude/.credentials.json` (OAuth token)
and `~/.claude.json` (`organizationUuid`). Override them with
`CLAUDE_RC_ACCESS_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_RC_ORG_UUID`.

## CLI

```bash
claude-rc whoami                 # login / org / token status
claude-rc list                   # your Remote Control sessions
claude-rc get   <cse_id>         # session details (JSON)
claude-rc events <cse_id>        # recent history
claude-rc watch <cse_id>         # stream live (read-only)
claude-rc send  <cse_id> "run the tests"   # send a message, print the reply
claude-rc repl  <cse_id>         # interactive chat
claude-rc web                    # browser control panel (all sessions)
```

## Web UI

A dependency-free browser control panel for your Remote Control sessions: list
them, watch the live event stream, send messages, and steer (interrupt / set
model / set permission mode / archive).

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

# discover sessions
for s in rc.sessions():
    print(s["id"], s["status"], s["title"])

sid = rc.sessions()[0]["id"]

# ask and wait for the answer (opens the stream first, sends, stops on the `result` event)
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
rc.set_model(sid, "claude-opus-4-8")
rc.set_permission_mode(sid, "acceptEdits")
rc.set_effort(sid, "high")  # low|medium|high|xhigh, or None = auto
```

Cloud (Managed Agents) mode:

```python
from claude_rc import ManagedAgentsClient
ma = ManagedAgentsClient(api_key="sk-ant-...")
agent = ma.create_agent(name="A", model="claude-opus-4-8",
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
types are `user`, `assistant`, `result` (turn complete), `control_request`
(steering / permission prompts), and `system` (init). Helpers:

```python
ev.type          # payload type
ev.role          # "user" | "assistant" | None
ev.text()        # concatenated text blocks (handles str or block-list content)
ev.tool_uses()   # tool_use content blocks
ev.sequence_num  # RC ordering/resume cursor (int)
ev.is_turn_end   # a `result` (RC) or `session.status_idle` (managed agents)
ev.is_blocking_control  # a permission prompt waiting on you
```

## Safety

- `list`/`get`/`events`/`watch` and all `RemoteControlClient` read methods are
  read-only. `send`/`repl`/`send_message`/`interrupt` inject into a live
  session, so only use them on sessions you own.
- Token refresh writes the refreshed token back to
  `~/.claude/.credentials.json` (same as the CLI). Pass `persist_refresh=False`
  to keep refreshes in memory.
- This project never prints or transmits your tokens.

## Used by

[`claude-remote-bridge`](https://github.com/ThatCrispyToast/g2-claude-remote)
(the `server/` half of the Claude Remote glasses app) depends on this package
straight from git and wraps `RemoteControlClient` in a JSON + SSE HTTP bridge. It
imports `client`, `credentials`, and `events`, and resolves this repo's default
branch on each fresh `uv` install — so keep those three backwards-compatible.

## Project layout

```
claude_rc/
  credentials.py   # OAuth load + refresh, org uuid, ~/.claude parsing
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
