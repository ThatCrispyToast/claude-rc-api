"""Command-line interface for the Claude Remote Control API.

    claude-rc whoami                 # show login / org / token status
    claude-rc list                   # your active sessions (--all adds archived, --pinned filters)
    claude-rc get <session_id>       # session details
    claude-rc events <session_id>    # recent events (history)
    claude-rc watch <session_id>     # stream events live (read-only)
    claude-rc info <session_id>      # summary, context, plan usage, git changes
    claude-rc ask <session_id> "what are you stuck on?"   # side question
    claude-rc send <session_id> "run the tests"   # send a message, print the reply
    claude-rc repl <session_id>      # interactive chat with a live session
    claude-rc rename <session_id> "new title"
    claude-rc pin <session_id>       # pin it in claude.ai/code's sidebar (unpin undoes it)
    claude-rc web                    # browser control panel for all sessions

`send`, `repl`, `rename`, `pin`, and `web` change live sessions — only use them
on sessions you started with `claude remote-control`. `ask` and `info` query the
worker (a side question costs a model call; neither touches the transcript).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from typing import Optional

from . import __version__
from .client import PINNED_TAG, APIError, ControlRejected, ControlTimeout, RemoteControlClient
from .credentials import CredentialsError, load_credentials, load_org_uuid

try:  # optional pretty output
    from rich.console import Console
    from rich.markup import escape as _escape
    from rich.table import Table

    _console: Optional["Console"] = Console()
except Exception:  # pragma: no cover - rich optional
    _console = None


def _print(*a, **k):
    if _console:
        _console.print(*a, **k)
    else:
        print(*[str(x) for x in a], **k)


def _esc(text: object) -> str:
    """Session content for _print: rich would read `[...]` in it as markup."""
    return _escape(str(text)) if _console else str(text)


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = _dt.datetime.now(_dt.timezone.utc) - t
        secs = int(delta.total_seconds())
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= n:
                return f"{secs // n}{unit} ago"
        return f"{secs}s ago"
    except Exception:
        return iso


# --- commands --------------------------------------------------------------
def cmd_whoami(args) -> int:
    try:
        creds = load_credentials()
    except CredentialsError as e:
        _print(f"not logged in: {e}")
        return 1
    org = load_org_uuid()
    _print(f"access token : {'present' if creds.access_token else 'MISSING'} (len {len(creds.access_token)})")
    _print(f"expired      : {creds.is_expired()}")
    _print(f"scopes       : {', '.join(creds.scopes) or '(unknown)'}")
    _print(f"subscription : {creds.subscription_type or '-'}")
    _print(f"org uuid     : {'present' if org else 'absent (optional: /v1/code/sessions works without it)'}")
    return 0


def _context_pct(session: dict) -> str:
    usage = (session.get("external_metadata") or {}).get("context_usage") or {}
    if not usage.get("max_tokens"):
        return "-"
    return f"{round(100 * usage.get('used_tokens', 0) / usage['max_tokens'])}%"


def cmd_list(args) -> int:
    with RemoteControlClient() as rc:
        sessions = rc.sessions(
            limit=args.limit,
            statuses=None if args.all else "active",
            tags=PINNED_TAG if args.pinned else None,
        )
    if _console:
        table = Table(title=f"{len(sessions)} Remote Control sessions")
        for col in ("id", "title", "status", "worker", "model", "effort", "ctx", "last activity"):
            table.add_column(col, overflow="fold")
        for s in sessions:
            pin = "★ " if PINNED_TAG in (s.get("tags") or []) else ""
            table.add_row(
                s.get("id", ""),
                _esc(pin + (s.get("title") or "")[:50]),
                str(s.get("status", "")),
                str(s.get("worker_status", "")),
                (s.get("config") or {}).get("model", ""),
                (s.get("external_metadata") or {}).get("effort_level") or "-",
                _context_pct(s),
                _fmt_time(s.get("last_event_at")),
            )
        _console.print(table)
    else:
        for s in sessions:
            print(f"{s.get('id')}\t{s.get('status')}\t{(s.get('title') or '')[:50]}")
    return 0


def cmd_get(args) -> int:
    import json

    with RemoteControlClient() as rc:
        print(json.dumps(rc.get_session(args.session_id), indent=2))
    return 0


def cmd_events(args) -> int:
    with RemoteControlClient() as rc:
        # The newest `limit` events, oldest first (`asc` would be the session's FIRST ones).
        evs = rc.list_events(args.session_id, limit=args.limit, sort_order="desc")
    for e in reversed(evs):
        _render_event(e)
    return 0


def cmd_watch(args) -> int:
    _print("streaming (Ctrl-C to stop)…")
    with RemoteControlClient() as rc:
        try:
            for e in rc.stream_events(args.session_id, from_sequence_num=args.from_seq):
                _render_event(e)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_info(args) -> int:
    with RemoteControlClient() as rc:
        s = rc.get_session(args.session_id)
        meta = s.get("external_metadata") or {}
        summary = meta.get("post_turn_summary") or {}
        _print(_esc(f"{s.get('title')}  ({s.get('status')} · {s.get('worker_status')})"))
        _print(f"model   : {(s.get('config') or {}).get('model', '-')}  effort {meta.get('effort_level') or '-'}")
        _print(f"context : {_context_pct(s)}")
        if summary:
            _print(_esc(f"summary : {summary.get('needs_action') or summary.get('status_detail') or '-'}"))
        if s.get("connection_status") != "connected":
            return 0  # the controls below need a connected worker
        usage = rc.get_usage(args.session_id)
        for limit in (usage.get("rate_limits") or {}).get("limits") or []:
            _print(f"plan    : {limit.get('kind')} {limit.get('percent')}% (resets {limit.get('resets_at')})")
        diff = rc.get_workspace_diff(args.session_id)
        if diff:
            st = diff.get("stats") or {}
            _print(f"changes : {st.get('filesCount', 0)} files +{st.get('linesAdded', 0)} -{st.get('linesRemoved', 0)}")
        _print(f"worker  : Claude Code {meta.get('container_cc_version', '?')}")
    return 0


def cmd_ask(args) -> int:
    with RemoteControlClient() as rc:
        answer = rc.side_question(args.session_id, args.question)
    _print(_esc(answer) if answer is not None else "(no answer — the session has no conversation yet)")
    return 0


def cmd_rename(args) -> int:
    with RemoteControlClient() as rc:
        _print(_esc(f"renamed → {rc.rename_session(args.session_id, args.title).get('title')}"))
    return 0


def cmd_pin(args) -> int:
    with RemoteControlClient() as rc:
        if args.cmd == "pin":
            done, session = "pinned", rc.pin_session(args.session_id)
        else:
            done, session = "unpinned", rc.unpin_session(args.session_id)
    _print(_esc(f"{done} → {session.get('title')}"))
    return 0


def cmd_send(args) -> int:
    with RemoteControlClient() as rc:
        for e in rc.send_and_collect(args.session_id, args.message):
            _render_event(e)
    return 0


def cmd_repl(args) -> int:
    _print("REPL — type a message and press enter; Ctrl-D to quit.")
    with RemoteControlClient() as rc:
        while True:
            try:
                text = input("you> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text.strip():
                continue
            for e in rc.send_and_collect(args.session_id, text):
                _render_event(e)


def cmd_web(args) -> int:
    from .webui import serve

    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        verbose=args.verbose,
    )
    return 0


def _render_event(e) -> None:
    t = e.type
    if e.role == "assistant":
        body = _esc(e.text().strip())
        if body:
            _print(f"[green]claude[/green]> {body}" if _console else f"claude> {body}")
        for tu in e.tool_uses():
            name = _esc(tu.get("name"))
            _print(f"  [yellow]· tool[/yellow] {name}" if _console else f"  · tool {name}")
    elif e.role == "user":
        body = _esc(e.text().strip())
        if body:
            _print(f"[cyan]user[/cyan]> {body}" if _console else f"user> {body}")
    elif t == "result":
        _print(f"[dim]— turn complete ({e.subtype})[/dim]" if _console else f"— turn complete ({e.subtype})")
    elif t == "system" and e.subtype == "init":
        model = e.payload.get("model", "?")
        _print(f"[dim]· session init (model={model})[/dim]" if _console else f"· session init (model={model})")
    elif e.is_blocking_control:
        what = _esc((e.payload.get("request") or {}).get("tool_name") or e.control_subtype)
        _print(f"[magenta]⚠ needs you: {what}[/magenta]" if _console else f"⚠ needs you: {what}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-rc", description="Claude Remote Control API client")
    p.add_argument("--version", action="version", version=f"claude-rc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="show login / org / token status").set_defaults(func=cmd_whoami)

    pl = sub.add_parser("list", help="list Remote Control sessions (active ones unless --all)")
    pl.add_argument("--limit", type=int, default=None, help="page size (server default 20, at most 200)")
    pl.add_argument("--all", action="store_true", help="include archived sessions")
    pl.add_argument("--pinned", action="store_true", help="only sessions pinned in claude.ai/code")
    pl.set_defaults(func=cmd_list)

    pg = sub.add_parser("get", help="session details (JSON)")
    pg.add_argument("session_id")
    pg.set_defaults(func=cmd_get)

    pe = sub.add_parser("events", help="recent events (history)")
    pe.add_argument("session_id")
    pe.add_argument("--limit", type=int, default=30)
    pe.set_defaults(func=cmd_events)

    pw = sub.add_parser("watch", help="stream events live (read-only)")
    pw.add_argument("session_id")
    pw.add_argument("--from-seq", type=int, default=0, dest="from_seq")
    pw.set_defaults(func=cmd_watch)

    pi = sub.add_parser("info", help="summary, context, plan usage, git changes")
    pi.add_argument("session_id")
    pi.set_defaults(func=cmd_info)

    pa = sub.add_parser("ask", help="ask a side question (no interruption, not in the transcript)")
    pa.add_argument("session_id")
    pa.add_argument("question")
    pa.set_defaults(func=cmd_ask)

    pn = sub.add_parser("rename", help="retitle a session")
    pn.add_argument("session_id")
    pn.add_argument("title")
    pn.set_defaults(func=cmd_rename)

    for name, what in (("pin", "pin a session in claude.ai/code's sidebar"), ("unpin", "remove a session's pin")):
        pp = sub.add_parser(name, help=what)
        pp.add_argument("session_id")
        pp.set_defaults(func=cmd_pin)

    ps = sub.add_parser("send", help="send a message and print the reply")
    ps.add_argument("session_id")
    ps.add_argument("message")
    ps.set_defaults(func=cmd_send)

    pr = sub.add_parser("repl", help="interactive chat with a live session")
    pr.add_argument("session_id")
    pr.set_defaults(func=cmd_repl)

    pweb = sub.add_parser("web", help="launch the browser control panel")
    pweb.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    pweb.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    pweb.add_argument("--no-open", action="store_true", help="don't open a browser window")
    pweb.add_argument("--verbose", action="store_true", help="log HTTP requests")
    pweb.set_defaults(func=cmd_web)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CredentialsError as e:
        _print(f"auth error: {e}")
        return 1
    except (ControlRejected, ControlTimeout) as e:
        _print(_esc(f"worker: {e}"))
        return 1
    except APIError as e:
        _print(_esc(f"api error: {e}"))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
