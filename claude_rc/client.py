"""Clients for the Claude Remote Control / Sessions HTTP API.

Two entry points:

* :class:`RemoteControlClient` — talks to ``/v1/code/sessions`` with a claude.ai
  OAuth token, exactly like the claude.ai/code web app. Use it to list, steer,
  and observe Claude Code sessions running on a machine that started
  ``claude remote-control`` (or ``claude --remote-control``).

* :class:`ManagedAgentsClient` — talks to the public ``/v1/sessions`` Managed
  Agents API with an ``x-api-key``. Use it to create and drive fully
  cloud-hosted agent sessions.

Both were reverse-engineered from the Claude Code CLI binary. Nothing here is an
officially supported interface; endpoints and headers can change without notice.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import httpx

from .credentials import (
    CredentialsError,
    OAuthCredentials,
    load_credentials,
    load_org_uuid,
    refresh_credentials,
)
from .events import (
    Event,
    cli_user_message,
    cli_control_request,
    user_message,
    custom_tool_result,
)
from .sse import parse_sse

DEFAULT_BASE_URL = "https://api.anthropic.com"
# Beta headers observed in the CLI.
CCR_BETA = "ccr-byoc-2025-07-29"          # /v1/sessions (v1) remote-control path
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
ANTHROPIC_VERSION = "2023-06-01"
# anthropic-client-platform value the claude.ai web app sends. Since Claude Code
# ~2.1.220 this header is load-bearing on ingest, not telemetry: CCR ingress
# stamps it into each ingested event's `client_platform`, and the worker treats
# a user message as coming from a *human controller* only when that value is
# one of `ios` / `android` / `web_claude_ai` / `desktop_app`. Any other value
# (including the `claude_code_remote` this library sent before 0.2.1) gets the
# message demoted to *peer* origin — "Another Claude session sent a message"
# framing — and, depending on the worker's peer-gate policy, parked without
# ever starting a turn (observed live on 2.1.234; 2.1.219 predates the gate).
CLIENT_PLATFORM = "web_claude_ai"
USER_AGENT = "claude-rc-api-python/0.2.1"


def _backoff_sleep(attempt: int, base: float = 0.5, cap: float = 30.0) -> None:
    time.sleep(min(cap, base * (2 ** (attempt - 1))))


class APIError(RuntimeError):
    def __init__(self, status: int, body: str, request_id: Optional[str] = None):
        self.status = status
        self.body = body
        self.request_id = request_id
        super().__init__(f"HTTP {status}{f' [{request_id}]' if request_id else ''}: {body[:300]}")


class ControlRejected(RuntimeError):
    """The worker answered a ``control_request`` with an error ``control_response``.

    The ingest POST succeeding only means the request was queued — the worker
    acks (or refuses) asynchronously in the event stream. E.g. remote-control
    REPL workers (observed through Claude Code 2.1.212) do not dispatch the
    ``apply_flag_settings`` subtype and answer
    ``"REPL bridge does not handle control_request subtype: …"``."""


# ---------------------------------------------------------------------------
# OAuth (Remote Control) client
# ---------------------------------------------------------------------------
class RemoteControlClient:
    """Controller-side client for Remote Control sessions (``/v1/code/sessions``).

    This plays the same role as the browser at claude.ai/code: it does not run
    Claude, it *steers* a session that is running on someone's machine.
    """

    def __init__(
        self,
        credentials: OAuthCredentials | None = None,
        org_uuid: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        credentials_path: str | None = None,
        config_path: str | None = None,
        auto_refresh: bool = True,
        persist_refresh: bool = True,
        client_id: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._creds = credentials or load_credentials(credentials_path)
        self._creds_path = credentials_path
        self._org_uuid = org_uuid or load_org_uuid(config_path)
        self._auto_refresh = auto_refresh
        self._persist_refresh = persist_refresh
        # A stable per-instance id for presence, like the web client.
        self.client_id = client_id or str(uuid.uuid4())
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, read=timeout))
        self._refresher = httpx.Client(timeout=30.0)

    # -- context management ------------------------------------------------
    def __enter__(self) -> "RemoteControlClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()
        self._refresher.close()

    # -- auth --------------------------------------------------------------
    def _reload_credentials(self) -> bool:
        """Re-read the credentials file; ``True`` if it held different tokens.

        Tokens are rotated *in place* by whichever process refreshes first (the
        Claude Code CLI itself, another client of this library, …), and a
        refresh token is single-use — once someone else has spent ours, the
        disk copy is the only valid one. A long-lived client must therefore
        treat the file, not its memory, as the source of truth. No-op when the
        credentials didn't come from a file (env var / explicit object)."""
        if not self._creds.source_path:
            return False
        try:
            fresh = load_credentials(self._creds_path)
        except CredentialsError:
            return False
        if (
            fresh.access_token == self._creds.access_token
            and fresh.refresh_token == self._creds.refresh_token
        ):
            return False
        self._creds = fresh
        return True

    def _token(self) -> str:
        if self._auto_refresh and self._creds.is_expired():
            # Prefer the disk copy: another process may already have rotated
            # the tokens, which invalidated our (single-use) refresh token.
            if not self._reload_credentials() or self._creds.is_expired():
                try:
                    self._creds = refresh_credentials(
                        self._creds, persist=self._persist_refresh, client=self._refresher
                    )
                except CredentialsError:
                    # Our refresh token was already spent (invalid_grant) —
                    # someone else may have written fresh tokens meanwhile.
                    if not self._reload_credentials() or self._creds.is_expired():
                        raise
        return self._creds.access_token

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-client-platform": CLIENT_PLATFORM,
            "User-Agent": USER_AGENT,
        }
        if self._org_uuid:
            h["x-organization-uuid"] = self._org_uuid
        if self._creds.trusted_device_token:
            h["X-Trusted-Device-Token"] = self._creds.trusted_device_token
        if extra:
            h.update(extra)
        return h

    def _url(self, *parts: str) -> str:
        tail = "/".join(str(p).strip("/") for p in parts if p != "")
        return f"{self.base_url}/v1/code/sessions" + (f"/{tail}" if tail else "")

    # -- low-level request with one 401->refresh retry ---------------------
    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        resp = self._http.request(method, url, headers=self._headers(kw.pop("extra_headers", None)), **kw)
        if resp.status_code == 401 and self._auto_refresh:
            # Recover and retry once, mirroring the CLI's bridge client. Fresh
            # tokens on disk (rotated by another process) win; otherwise spend
            # our refresh token.
            renewed = self._reload_credentials()
            if not renewed and self._creds.refresh_token:
                try:
                    self._creds = refresh_credentials(
                        self._creds, persist=self._persist_refresh, client=self._refresher
                    )
                    renewed = True
                except CredentialsError:
                    renewed = self._reload_credentials()
                    if not renewed:
                        raise
            if renewed:
                resp = self._http.request(method, url, headers=self._headers(), **kw)
        return resp

    @staticmethod
    def _ok(resp: httpx.Response, *accept: int) -> Any:
        accepted = accept or (200, 201, 204)
        if resp.status_code not in accepted:
            rid = resp.headers.get("request-id") or resp.headers.get("x-request-id")
            raise APIError(resp.status_code, resp.text, rid)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return resp.text

    # -- sessions ----------------------------------------------------------
    def list_sessions(self, *, limit: int | None = None, page: str | None = None) -> dict:
        """``GET /v1/code/sessions`` — your Remote Control sessions."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if page is not None:
            params["page"] = page
        return self._ok(self._request("GET", self._url(), params=params))

    def sessions(self, **kw) -> list[dict]:
        """Convenience: just the list of session objects from :meth:`list_sessions`."""
        body = self.list_sessions(**kw)
        if isinstance(body, list):
            return body
        for key in ("data", "sessions", "results", "items"):
            if isinstance(body.get(key), list):
                return body[key]
        return []

    def get_session(self, session_id: str) -> dict:
        """``GET /v1/code/sessions/{id}``."""
        return self._ok(self._request("GET", self._url(session_id)))

    def archive_session(self, session_id: str) -> bool:
        """``POST /v1/code/sessions/{id}/archive`` — end/hide a session. 409 = already archived."""
        resp = self._request("POST", self._url(session_id, "archive"), json={})
        if resp.status_code in (200, 409):
            return True
        self._ok(resp)  # raises
        return False

    # -- sending events (steering) ----------------------------------------
    def send_events(self, session_id: str, events: Iterable[dict]) -> dict:
        """``POST /v1/code/sessions/{id}/events``.

        Body is ``{session_id, events:[{payload: <event+uuid>}]}`` — each event is
        wrapped in ``payload`` and given a ``uuid`` if it lacks one (matching the CLI).
        """
        wrapped = []
        for e in events:
            if "uuid" not in e:
                e = {**e, "uuid": str(uuid.uuid4())}
            wrapped.append({"payload": e})
        body = {"session_id": session_id, "events": wrapped}
        return self._ok(self._request("POST", self._url(session_id, "events"), json=body), 200, 201)

    def send_message(self, session_id: str, text: str) -> dict:
        """Send a user message (Claude Code stream-json ``user`` event) — what you
        type at claude.ai/code."""
        return self.send_events(session_id, [cli_user_message(text)])

    def send_raw(self, session_id: str, payload: dict) -> dict:
        """Send an arbitrary stream-json payload (advanced escape hatch)."""
        return self.send_events(session_id, [payload])

    def interrupt(self, session_id: str) -> dict:
        """Interrupt the running agent via a ``control_request`` (jumps the queue)."""
        req = cli_control_request("interrupt", request_id=f"interrupt-{uuid.uuid4()}")
        return self.send_events(session_id, [req])

    def set_model(self, session_id: str, model: str) -> dict:
        """Change the session's model (e.g. ``claude-opus-4-8``)."""
        req = cli_control_request("set_model", request_id=f"set-model-{uuid.uuid4()}", model=model)
        return self.send_events(session_id, [req])

    def set_permission_mode(self, session_id: str, mode: str) -> dict:
        """Set permission mode: ``default`` | ``plan`` | ``acceptEdits`` | ``bypassPermissions``."""
        req = cli_control_request(
            "set_permission_mode", request_id=f"set-mode-{uuid.uuid4()}", mode=mode
        )
        return self.send_events(session_id, [req])

    def set_effort(
        self,
        session_id: str,
        effort: str | None,
        *,
        ultracode: bool = False,
        wait: float | None = None,
        command_fallback: bool = False,
    ) -> dict:
        """Set reasoning effort: ``low`` | ``medium`` | ``high`` | ``xhigh``; ``None`` = auto.

        There is no ``set_effort`` control subtype — the CLI's ``/effort`` command
        sends ``apply_flag_settings`` with ``settings.effortLevel`` (an explicit
        ``null`` clears back to auto) and ``ultracode`` alongside it (a plain
        effort change switches ultracode off). ``max`` is session-scoped in the
        CLI and rejected by the worker's flag-settings schema, so it cannot be
        applied remotely. ``ultracode=True`` needs ``effort="xhigh"`` and an
        xhigh-capable model on the worker.

        **Remote-control REPL workers (observed through 2.1.212) do not dispatch
        ``apply_flag_settings``** — they refuse it with an error
        ``control_response`` (SDK/cloud workers apply it). Hence the two opt-ins:

        * ``wait`` (seconds) — poll for the worker's ``control_response`` and act
          on the verdict instead of firing blind. Without it, a refusal only ever
          surfaces in the event stream.
        * ``command_fallback`` — on refusal, inject ``/effort <level>`` as a user
          message instead. Remote-control workers execute slash commands from
          controllers as local commands (verified live: zero cost, no model
          turn) — but mind the CLI semantics: a plain level is also *persisted
          as that machine's default for new sessions*, not session-scoped.
          Requires ``wait``. Without it a refusal raises :class:`ControlRejected`.

        Returns ``{"via": "control" | "command" | "control_unconfirmed", "ack": …}``
        — ``control_unconfirmed`` means ``wait`` elapsed with no verdict (assumed
        applied; no fallback is attempted, to avoid a double apply).
        """
        request_id = f"set-effort-{uuid.uuid4()}"
        req = cli_control_request(
            "apply_flag_settings",
            request_id=request_id,
            settings={"effortLevel": effort, "ultracode": ultracode},
        )
        ack = self.send_events(session_id, [req])
        if wait is None:
            return {"via": "control", "ack": ack}
        resp = self.wait_control_response(session_id, request_id, timeout=wait)
        if resp is None:
            return {"via": "control_unconfirmed", "ack": ack}
        if resp.get("subtype") != "error":
            return {"via": "control", "ack": ack}
        if not command_fallback:
            raise ControlRejected(str(resp.get("error") or "control request rejected"))
        command = "ultracode" if ultracode else (effort or "auto")
        ack = self.send_message(session_id, f"/effort {command}")
        return {"via": "command", "ack": ack}

    def wait_control_response(
        self,
        session_id: str,
        request_id: str,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.4,
    ) -> dict | None:
        """Poll history for the worker's ``control_response`` to ``request_id``.

        Returns the ``response`` object (``subtype`` is ``success`` | ``error``),
        or ``None`` if none arrived within ``timeout``. Controls are acked
        asynchronously through the event stream — a 200 on the ingest POST only
        means "queued", so this is how a caller learns whether the worker
        actually applied one.
        """
        deadline = time.monotonic() + timeout
        while True:
            for ev in self.list_events(session_id, limit=25, sort_order="desc"):
                if ev.type != "control_response":
                    continue
                resp = ev.payload.get("response") or {}
                if resp.get("request_id") == request_id:
                    return resp
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)

    def mark_read(self, session_id: str, up_to_sequence_num: int | None = None) -> Any:
        """``POST /v1/code/sessions/{id}/mark_read`` — mark the session read."""
        body = {}
        if up_to_sequence_num is not None:
            body["sequence_num"] = up_to_sequence_num
        return self._ok(self._request("POST", self._url(session_id, "mark_read"), json=body), 200, 204)

    # -- reading events: polling ------------------------------------------
    def list_events(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        sort_order: str | None = None,
        from_sequence_num: int | None = None,
        page: str | None = None,
    ) -> list[Event]:
        """``GET /v1/code/sessions/{id}/events`` — paginated history (returns immediately).

        ``sort_order`` is ``asc`` | ``desc`` (the CLI fetches ``desc`` = newest first).
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if sort_order is not None:
            params["sort_order"] = sort_order
        if from_sequence_num is not None:
            params["from_sequence_num"] = from_sequence_num
        if page is not None:
            params["page"] = page
        body = self._ok(self._request("GET", self._url(session_id, "events"), params=params))
        rows = body if isinstance(body, list) else body.get("data") or body.get("events") or []
        return [Event.from_wire(r) for r in rows]

    # -- reading events: streaming (SSE) ----------------------------------
    def stream_events(
        self,
        session_id: str,
        *,
        from_sequence_num: int = 0,
        reconnect: bool = True,
        max_reconnects: int = 20,
    ) -> Iterator[Event]:
        """``GET /v1/code/sessions/{id}/events/stream`` — live SSE events.

        Each SSE frame is a ``client_event`` envelope
        ``{event_type, sequence_num, source, payload}``; this unwraps ``payload``
        into an :class:`Event`. Resumes with both ``?from_sequence_num=`` and a
        ``Last-Event-ID`` header, de-duping by ``sequence_num``, so a dropped
        connection neither replays nor skips events. Reconnects with exponential
        backoff. Open this *before* sending a message — the stream only delivers
        events that occur after it connects.
        """
        seen: set[int] = set()
        last_seq = from_sequence_num
        attempts = 0
        while True:
            params: dict[str, Any] = {"from_sequence_num": last_seq}
            headers = self._headers({"Accept": "text/event-stream"})
            if last_seq:
                headers["Last-Event-ID"] = str(last_seq)
            url = self._url(session_id, "events", "stream")
            try:
                with self._http.stream(
                    "GET", url, headers=headers, params=params,
                    timeout=httpx.Timeout(30.0, read=None),
                ) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode(errors="replace")
                        raise APIError(resp.status_code, body, resp.headers.get("request-id"))
                    attempts = 0
                    for frame in parse_sse(resp.iter_lines()):
                        if frame.is_heartbeat:
                            continue
                        # The RC stream marks each frame's sequence_num on the id: line.
                        if frame.id:
                            try:
                                last_seq = max(last_seq, int(frame.id))
                            except ValueError:
                                pass
                        # Ignore purely informational top-level events.
                        if frame.event in ("session_update", "delivery_update"):
                            continue
                        try:
                            data = json.loads(frame.data)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # `catch_up_truncated` signals a history gap; surface it raw.
                        if frame.event == "catch_up_truncated" or "payload" not in data:
                            yield Event.from_wire(data)
                            continue
                        ev = Event.from_wire(data)
                        if ev.sequence_num is not None:
                            if ev.sequence_num in seen:
                                continue
                            seen.add(ev.sequence_num)
                            last_seq = max(last_seq, ev.sequence_num)
                        yield ev
            except httpx.HTTPError:
                if not reconnect or attempts >= max_reconnects:
                    raise
                attempts += 1
                _backoff_sleep(attempts)
                continue
            if not reconnect:
                return

    # -- presence ----------------------------------------------------------
    def presence(self, session_id: str, *, clear: bool = False) -> None:
        """``POST /v1/code/sessions/{id}/client/presence`` — announce (or clear) that a
        controller is watching. The web client pulses this periodically."""
        if clear:
            payload = {"client_id": self.client_id, "clear": True}
        else:
            payload = {
                "client_id": self.client_id,
                "connected_at": datetime.now(timezone.utc).isoformat(),
            }
        self._ok(self._request("POST", self._url(session_id, "client", "presence"), json=payload), 200, 204)

    # -- high-level convenience -------------------------------------------
    def send_and_collect(
        self,
        session_id: str,
        text: str,
        *,
        print_stream: bool = False,
        idle_timeout: float | None = None,
    ) -> list[Event]:
        """Send a message and stream until the session goes idle again.

        Returns every event received during the turn. This is the "ask and wait
        for the answer" pattern.

        Nothing is lost to the gap between sending and connecting. :meth:`stream_events`
        is a generator, so the HTTP connection only opens on the first iteration
        below — *after* the send — but the stream is resumed from the newest
        sequence number that existed beforehand, so the server replays anything
        that happened in between.
        """
        # Seed the resume cursor from the newest event so we only see new ones.
        try:
            history = self.list_events(session_id, limit=1, sort_order="desc")
            start = max((e.sequence_num or 0) for e in history) if history else 0
        except APIError:
            start = 0

        collected: list[Event] = []
        stream = self.stream_events(session_id, from_sequence_num=start, reconnect=False)
        self.send_message(session_id, text)
        for ev in stream:
            collected.append(ev)
            if print_stream and ev.role == "assistant":
                t = ev.text()
                if t:
                    print(t)
            # Stop when the turn ends, the session dies, or it blocks on us
            # (a permission prompt / dialog that needs a controller response).
            if ev.is_turn_end or ev.is_terminal or ev.is_blocking_control:
                break
        return collected


# ---------------------------------------------------------------------------
# API-key (public Managed Agents) client
# ---------------------------------------------------------------------------
class ManagedAgentsClient:
    """Client for the public Managed Agents Sessions API (``/v1/sessions``).

    Unlike Remote Control, sessions here run in Anthropic's cloud. You must
    create an agent (and usually an environment) first. Events are sent
    *unwrapped* (``{"events": [event, ...]}``) and the SSE stream frames carry
    the event object directly (``id``/``processed_at``, no ``sequence_num``).
    """

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, read=timeout))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._http.close()

    def close(self):
        self._http.close()

    def _headers(self, betas: list[str] | None = None, extra: dict | None = None) -> dict:
        beta = ",".join([MANAGED_AGENTS_BETA, *(betas or [])])
        h = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": beta,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if extra:
            h.update(extra)
        return h

    def _ok(self, resp: httpx.Response, *accept: int) -> Any:
        accepted = accept or (200, 201, 204)
        if resp.status_code not in accepted:
            rid = resp.headers.get("request-id")
            raise APIError(resp.status_code, resp.text, rid)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- agents / environments --------------------------------------------
    def create_agent(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/agents", headers=self._headers(), json=body))

    def create_environment(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/environments", headers=self._headers(), json=body))

    # -- sessions ----------------------------------------------------------
    def create_session(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/sessions", headers=self._headers(), json=body))

    def list_sessions(self, **params) -> dict:
        return self._ok(self._http.get(f"{self.base_url}/v1/sessions", headers=self._headers(), params=params))

    def get_session(self, session_id: str) -> dict:
        return self._ok(self._http.get(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers()))

    def archive_session(self, session_id: str) -> Any:
        return self._ok(self._http.post(f"{self.base_url}/v1/sessions/{session_id}/archive", headers=self._headers(), json={}))

    def delete_session(self, session_id: str) -> Any:
        return self._ok(self._http.delete(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers()))

    # -- events ------------------------------------------------------------
    def send_events(self, session_id: str, events: list[dict]) -> dict:
        return self._ok(
            self._http.post(
                f"{self.base_url}/v1/sessions/{session_id}/events",
                headers=self._headers(),
                json={"events": events},
            )
        )

    def send_message(self, session_id: str, text: str) -> dict:
        return self.send_events(session_id, [user_message(text)])

    def list_events(self, session_id: str, **params) -> list[Event]:
        body = self._ok(
            self._http.get(f"{self.base_url}/v1/sessions/{session_id}/events", headers=self._headers(), params=params)
        )
        rows = body.get("data") if isinstance(body, dict) else body
        return [Event.from_wire(r) for r in (rows or [])]

    def stream_events(self, session_id: str, *, event_deltas: list[str] | None = None) -> Iterator[Event]:
        params = {}
        if event_deltas:
            params["event_deltas[]"] = event_deltas
        with self._http.stream(
            "GET",
            f"{self.base_url}/v1/sessions/{session_id}/events/stream",
            headers=self._headers(extra={"Accept": "text/event-stream"}),
            params=params,
            timeout=httpx.Timeout(30.0, read=None),
        ) as resp:
            if resp.status_code != 200:
                # Read the body first: on a streaming response `_ok` would touch
                # `.text` before it exists and raise httpx.ResponseNotRead
                # instead of the APIError callers are told to expect.
                body = resp.read().decode(errors="replace")
                raise APIError(resp.status_code, body, resp.headers.get("request-id"))
            for frame in parse_sse(resp.iter_lines()):
                if frame.is_heartbeat:
                    continue
                try:
                    yield Event.from_wire(json.loads(frame.data))
                except json.JSONDecodeError:
                    continue
