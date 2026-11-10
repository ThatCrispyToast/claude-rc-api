"""A tiny, dependency-free web control panel for Remote Control sessions.

Serves a single-page UI that lists your Remote Control sessions, watches the
live event stream of one, sends messages, asks side questions, answers
permission prompts, and steers it (interrupt / model / permission mode / effort /
pin / archive) — all on top of :class:`~claude_rc.client.RemoteControlClient`.

It is built on the standard-library :mod:`http.server`, so it adds **no new
dependencies** beyond what the client already needs. Launch it with::

    claude-rc web            # opens http://127.0.0.1:8765

or programmatically::

    from claude_rc.webui import serve
    serve(host="127.0.0.1", port=8765, open_browser=True)

Security: the server talks to the private API with *your* OAuth token and does
no authentication of its own, so it binds to ``127.0.0.1`` by default. Anyone who
can reach the bound address can read and steer your sessions — only expose it
beyond localhost if you know what you are doing.

HTTP surface (all JSON unless noted):

    GET  /                                    the single-page UI
    GET  /api/whoami                          login / org / token status
    GET  /api/sessions?all=                   {"sessions": [...]} (active ones; all=1 adds archived)
    GET  /api/sessions/{id}                   session detail
    GET  /api/sessions/{id}/events?limit=     {"events": [...]} (the newest, oldest first)
    GET  /api/sessions/{id}/stream?from_seq=  live events as text/event-stream
    POST /api/sessions/{id}/send              {"text": ...}
    POST /api/sessions/{id}/aside             {"question": ...} → {"answer": ...}
    POST /api/sessions/{id}/permission        {"request_id", "allow", "tool_input"?, "tool_use_id"?}
    POST /api/sessions/{id}/interrupt
    POST /api/sessions/{id}/model             {"model": ...}
    POST /api/sessions/{id}/permission_mode   {"mode": ...}
    POST /api/sessions/{id}/effort            {"effort": "low".."max" | null}
    POST /api/sessions/{id}/mark_read         {"event_id": ...?}
    POST /api/sessions/{id}/pin               {"pinned": true | false}
    POST /api/sessions/{id}/archive
    POST /api/sessions/{id}/unarchive
"""

from __future__ import annotations

import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .client import APIError, ControlRejected, ControlTimeout, RemoteControlClient
from .credentials import CredentialsError, load_credentials, load_org_uuid
from .events import Event

_STATIC = Path(__file__).parent / "static"
_PAGE = _STATIC / "index.html"
_LIST_LIMIT = 50  # sessions per sidebar fetch

# --- route patterns (path is matched against these, first hit wins) --------
_SID = r"(?P<sid>[^/]+)"
_R_SESSION = re.compile(rf"^/api/sessions/{_SID}$")
_R_EVENTS = re.compile(rf"^/api/sessions/{_SID}/events$")
_R_STREAM = re.compile(rf"^/api/sessions/{_SID}/stream$")
_R_SEND = re.compile(rf"^/api/sessions/{_SID}/send$")
_R_ASIDE = re.compile(rf"^/api/sessions/{_SID}/aside$")
_R_PERMISSION = re.compile(rf"^/api/sessions/{_SID}/permission$")
_R_INTERRUPT = re.compile(rf"^/api/sessions/{_SID}/interrupt$")
_R_MODEL = re.compile(rf"^/api/sessions/{_SID}/model$")
_R_PERM = re.compile(rf"^/api/sessions/{_SID}/permission_mode$")
_R_EFFORT = re.compile(rf"^/api/sessions/{_SID}/effort$")
_R_MARK = re.compile(rf"^/api/sessions/{_SID}/mark_read$")
_R_PIN = re.compile(rf"^/api/sessions/{_SID}/pin$")
_R_ARCHIVE = re.compile(rf"^/api/sessions/{_SID}/(?P<action>archive|unarchive)$")


def event_to_dict(ev: Event) -> dict:
    """Flatten an :class:`Event` into the JSON shape the UI consumes."""
    request = ev.payload.get("request") or {} if ev.is_blocking_control else {}
    return {
        "type": ev.type,
        "subtype": ev.subtype,
        "role": ev.role,
        "source": ev.source,
        "text": ev.text(),
        "tool_uses": [
            {"name": t.get("name"), "input": t.get("input"), "id": t.get("id")}
            for t in ev.tool_uses()
        ],
        "sequence_num": ev.sequence_num,
        "id": ev.id,
        "timestamp": ev.created_at or ev.processed_at,
        "model": ev.payload.get("model") if ev.type == "system" else None,
        "worker_state": ev.worker_state or None,
        "is_turn_end": ev.is_turn_end,
        "is_terminal": ev.is_terminal,
        "is_blocking_control": ev.is_blocking_control,
        "settles_prompt": ev.settles_prompt,
        "blocking_subtype": ev.control_subtype if ev.is_blocking_control else None,
        "request_id": ev.request_id,
        "tool_name": request.get("tool_name"),
        "tool_input": request.get("input"),
        "tool_use_id": request.get("tool_use_id"),
    }


class _RCServer(ThreadingHTTPServer):
    daemon_threads = True  # let long-lived SSE streams die with the process

    def __init__(self, addr, handler, client: RemoteControlClient, verbose: bool):
        super().__init__(addr, handler)
        self.rc = client
        self.verbose = verbose


class _Handler(BaseHTTPRequestHandler):
    server_version = "claude-rc-webui"
    protocol_version = "HTTP/1.1"

    # -- convenience -------------------------------------------------------
    @property
    def rc(self) -> RemoteControlClient:
        return self.server.rc  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _qint(self, q: dict, key: str, default: int) -> int:
        try:
            return int(q.get(key, [default])[0])
        except (ValueError, TypeError):
            return default

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, APIError):
            status = exc.status if 400 <= exc.status < 600 else 502
            self._json({"error": exc.body[:1000], "status": exc.status}, status=status)
        elif isinstance(exc, CredentialsError):
            self._json({"error": str(exc)}, status=401)
        elif isinstance(exc, ControlRejected):
            self._json({"error": str(exc)}, status=502)  # the worker refused it
        elif isinstance(exc, ControlTimeout):
            self._json({"error": str(exc)}, status=504)  # the worker never answered
        else:
            self._json({"error": repr(exc)}, status=500)

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_page()
            if path == "/api/whoami":
                return self._json(self._whoami())
            if path == "/api/sessions":
                everything = self._query().get("all", ["0"])[0] not in ("", "0")
                sessions = self.rc.sessions(limit=_LIST_LIMIT, statuses=None if everything else "active")
                return self._json({"sessions": sessions})
            m = _R_STREAM.match(path)
            if m:
                return self._stream(m["sid"])
            m = _R_EVENTS.match(path)
            if m:
                # The newest `limit` events, oldest first (an `asc` fetch would
                # return the session's FIRST events instead).
                limit = self._qint(self._query(), "limit", 200)
                evs = self.rc.list_events(m["sid"], limit=limit, sort_order="desc")
                return self._json({"events": [event_to_dict(e) for e in reversed(evs)]})
            m = _R_SESSION.match(path)
            if m:
                return self._json(self.rc.get_session(m["sid"]))
            self._json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        # Always consume the request body first: leaving bytes unread would
        # desync the next request on this keep-alive connection.
        body = self._body()
        try:
            m = _R_SEND.match(path)
            if m:
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json({"error": "empty message"}, status=400)
                self.rc.send_message(m["sid"], text)
                return self._json({"ok": True})
            m = _R_ASIDE.match(path)
            if m:
                question = (body.get("question") or "").strip()
                if not question:
                    return self._json({"error": "empty question"}, status=400)
                return self._json({"answer": self.rc.side_question(m["sid"], question, timeout=180)})
            m = _R_PERMISSION.match(path)
            if m:
                if not body.get("request_id"):
                    return self._json({"error": "missing request_id"}, status=400)
                self.rc.respond_permission(
                    m["sid"],
                    body["request_id"],
                    allow=bool(body.get("allow")),
                    tool_input=body.get("tool_input"),
                    tool_use_id=body.get("tool_use_id"),
                )
                return self._json({"ok": True})
            m = _R_INTERRUPT.match(path)
            if m:
                self.rc.interrupt(m["sid"])
                return self._json({"ok": True})
            m = _R_MODEL.match(path)
            if m:
                model = (body.get("model") or "").strip()
                if not model:
                    return self._json({"error": "missing model"}, status=400)
                self.rc.set_model(m["sid"], model)
                return self._json({"ok": True})
            m = _R_PERM.match(path)
            if m:
                mode = (body.get("mode") or "").strip()
                if not mode:
                    return self._json({"error": "missing mode"}, status=400)
                self.rc.set_permission_mode(m["sid"], mode)
                return self._json({"ok": True})
            m = _R_EFFORT.match(path)
            if m:
                effort = body.get("effort") or None
                out = self.rc.set_effort(m["sid"], effort, wait=6.0, command_fallback=True)
                return self._json({"ok": True, "via": out["via"]})
            m = _R_MARK.match(path)
            if m:
                event_id = body.get("event_id")
                self.rc.mark_read(m["sid"], event_id if isinstance(event_id, str) else None)
                return self._json({"ok": True})
            m = _R_PIN.match(path)
            if m:
                change = self.rc.pin_session if body.get("pinned", True) else self.rc.unpin_session
                return self._json({"ok": True, "tags": change(m["sid"]).get("tags") or []})
            m = _R_ARCHIVE.match(path)
            if m:
                if m["action"] == "archive":
                    self.rc.archive_session(m["sid"])
                else:
                    self.rc.unarchive_session(m["sid"])
                return self._json({"ok": True})
            self._json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # -- handlers ----------------------------------------------------------
    def _serve_page(self) -> None:
        try:
            html = _PAGE.read_bytes()
        except OSError:
            return self._json({"error": f"UI not found at {_PAGE}"}, status=500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _whoami(self) -> dict:
        try:
            creds = load_credentials()
        except CredentialsError as exc:
            return {"logged_in": False, "error": str(exc)}
        org = load_org_uuid()
        return {
            "logged_in": True,
            "expired": creds.is_expired(),
            "scopes": creds.scopes,
            "subscription": creds.subscription_type,
            "org_uuid_present": bool(org),
            "token_len": len(creds.access_token),
        }

    def _stream(self, sid: str) -> None:
        from_seq = self._qint(self._query(), "from_seq", 0)
        # An EventSource auto-reconnect resumes from the last id: we emitted.
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id and last_event_id.isdigit():
            from_seq = max(from_seq, int(last_event_id))

        # A stream has no Content-Length; close the socket when it ends so the
        # client sees a clean end-of-response instead of a stalled keep-alive.
        # (No `Connection: keep-alive` header: send_header special-cases it and
        # would flip close_connection back to False.)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
        self.end_headers()
        self.wfile.write(b"retry: 3000\n: connected\n\n")
        self.wfile.flush()

        try:
            for ev in self.rc.stream_events(sid, from_sequence_num=from_seq):
                data = json.dumps(event_to_dict(ev))
                out = ""
                if ev.sequence_num is not None:
                    out += f"id: {ev.sequence_num}\n"
                out += f"data: {data}\n\n"
                self.wfile.write(out.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # client navigated away / closed the tab
        except APIError as exc:
            self._sse_error(exc.body[:500], exc.status)
        except Exception as exc:  # noqa: BLE001 - headers already sent; can't reply twice
            self._sse_error(repr(exc), 500)

    def _sse_error(self, message: str, status: int) -> None:
        try:
            payload = json.dumps({"error": message, "status": status})
            self.wfile.write(f"event: error\ndata: {payload}\n\n".encode())
            self.wfile.flush()
        except OSError:
            pass


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    open_browser: bool = False,
    client: Optional[RemoteControlClient] = None,
    verbose: bool = False,
) -> None:
    """Run the control panel until interrupted (Ctrl-C).

    Binds to ``host:port`` and serves the single-page UI plus its JSON/SSE API.
    Reuses ``client`` if given, otherwise constructs a :class:`RemoteControlClient`
    from your ``~/.claude`` credentials.
    """
    rc = client or RemoteControlClient()
    httpd = _RCServer((host, port), _Handler, rc, verbose)
    url = f"http://{host if host not in ('0.0.0.0', '::') else '127.0.0.1'}:{port}/"
    print(f"claude-rc web UI  →  {url}   (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  ⚠ bound to a non-local address: anyone who can reach it can read and "
            "steer your Claude sessions with your credentials."
        )
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless boxes have no browser
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        if client is None:
            rc.close()
