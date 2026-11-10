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
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Optional

import httpx

from ._version import __version__
from . import credentials as _credentials
from .credentials import (
    ENV_TRUSTED_DEVICE_TOKEN,
    CredentialsError,
    OAuthCredentials,
    _stored_device_token,
    enroll_trusted_device,
    load_credentials,
    load_org_uuid,
    refresh_credentials,
)
from .events import (
    RC,
    Event,
    _to_int,
    cli_control_cancel_request,
    cli_control_request,
    cli_permission_response,
    cli_question_response,
    cli_user_message,
    user_message,
)
from .sse import SSEFrame, parse_sse

DEFAULT_BASE_URL = "https://api.anthropic.com"
# Beta headers observed in the CLI. The /v1/code/* paths need none; CCR_BETA rides
# only the CLI's older /v1/sessions compat path for the same sessions.
CCR_BETA = "ccr-byoc-2025-07-29"
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
USER_AGENT = f"claude-rc-api-python/{__version__}"
# The session tag claude.ai/code shows as a pin.
PINNED_TAG = "ui:pinned"

# The streams send a `:keepalive` comment about every 15 s. One silent for this
# long is dead, whatever the socket says: redial it (the CLI's own limit).
_STREAM_SILENCE_S = 45.0

# Waiting for a control verdict: the history page size each poll scans, and how
# far the poll interval backs off for slow verdicts (a side question can take
# minutes).
_VERDICT_SCAN = 50
_MAX_POLL_INTERVAL = 2.0

# The CLI's floor between lazy trusted-device enrollments, per process (each
# one emails the account owner).
_ENROLL_RETRY_S = 300.0
_enrollment_lock = threading.Lock()
_last_enrollment = float("-inf")  # time.monotonic()


def _may_try_enrolling() -> bool:
    """Claim this process's next lazy enrollment, if the floor allows one."""
    global _last_enrollment
    with _enrollment_lock:
        now = time.monotonic()
        if now - _last_enrollment < _ENROLL_RETRY_S:
            return False
        _last_enrollment = now
        return True


def _backoff_sleep(attempt: int, base: float = 0.5, cap: float = 30.0) -> None:
    time.sleep(min(cap, base * (2 ** (attempt - 1))))


def _ack_sequence(ack: Any) -> Optional[int]:
    """The sequence number ingest assigned the first event of a send."""
    results = ack.get("results") if isinstance(ack, dict) else None
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return _to_int(results[0].get("sequence_num"))
    return None


def _device_token_refused(body: str) -> bool:
    """A 403 body that refuses the trusted-device token itself, the one kind
    the CLI answers by enrolling: ``error.resource`` ``untrusted_device`` with
    no ``sub_reason``, or ``bound_unattested``. ``served_call_unsigned`` wants a
    device signature, which no token gives, and unknown sub-reasons are left
    alone as well."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return False
    return (
        isinstance(error, dict)
        and error.get("resource") == "untrusted_device"
        and error.get("sub_reason") in (None, "", "bound_unattested")
    )


def _unwrap_session(body: Any) -> Any:
    """Session reads and updates wrap the object (``{response_shape: …}`` /
    ``{session: …}``); hand callers the session itself."""
    if isinstance(body, dict):
        for key in ("response_shape", "session"):
            if isinstance(body.get(key), dict):
                return body[key]
    return body


class APIError(RuntimeError):
    def __init__(self, status: int, body: str, request_id: Optional[str] = None):
        self.status = status
        self.body = body
        self.request_id = request_id
        super().__init__(f"HTTP {status}{f' [{request_id}]' if request_id else ''}: {body[:300]}")


class ControlRejected(RuntimeError):
    """The worker answered a ``control_request`` with an error ``control_response``.

    The ingest POST succeeding only means the request was queued — the worker
    acks (or refuses) asynchronously in the event stream. A subtype the worker
    doesn't serve comes back as ``"REPL bridge does not handle control_request
    subtype: …"``. ``error_code`` is the worker's machine-readable reason when
    it gives one (``workspace_diff_pending``, ``invalid_mode`` …)."""

    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


class ControlTimeout(TimeoutError):
    """No ``control_response`` to a ``control_request`` arrived in time — the
    worker may be busy, gone, or on a version that drops the subtype."""

    def __init__(self, request_id: str, message: str):
        super().__init__(message)
        self.request_id = request_id


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
        # Where rotated tokens are re-read from: the path given, else a
        # non-default file explicit credentials came from, else (None) the
        # CLI's own lookup (on macOS the Keychain first, then the default file).
        source = credentials.source_path if credentials else None
        if source == _credentials.DEFAULT_CREDENTIALS_PATH:
            source = None
        self._creds_path = credentials_path or source
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
        """Re-read the credential store; ``True`` if it held different tokens.

        Tokens are rotated *in place* by whichever process refreshes first (the
        Claude Code CLI itself, another client of this library, …), and a
        refresh token is single-use — once someone else has spent ours, the
        stored copy is the only valid one. A long-lived client must therefore
        treat the store (the credentials file, or the macOS Keychain item), not
        its memory, as the source of truth. No-op when the credentials didn't
        come from a store (env var / explicit object)."""
        if not (self._creds.source_path or self._creds.source_keychain):
            return False
        try:
            fresh = load_credentials(self._creds_path)
        except CredentialsError:
            return False
        stale = self._creds.stale_refresh_token
        if stale and fresh.refresh_token in (stale, ""):
            return False  # the store is behind an unsaved refresh: ours are the live tokens
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

    def _renew_device_token(self, body: str) -> bool:
        """Recover from a 403 ``untrusted_device`` the way the CLI does: True
        when there is a different trusted-device token to retry with.

        Only a refusal of the token itself qualifies (see
        :func:`_device_token_refused`), and nothing changes while
        ``CLAUDE_TRUSTED_DEVICE_TOKEN`` is set, as with the CLI. If the store
        has moved on (a sign-in, rotated tokens), the retry goes out as its
        login first. A token the store gained meanwhile (the CLI enrolled) is
        taken as is. Otherwise this machine is enrolled for the login the store
        holds (:func:`~claude_rc.credentials.enroll_trusted_device`, which
        emails the account owner), with the CLI's guards against a loop: one
        attempt per process every 5 minutes at most, and none while the
        refused token is one a refusal enrolled less than a day ago. That guard
        lives in the store, so enrolling needs credentials from one and
        ``persist_refresh``.
        """
        creds = self._creds
        if not _device_token_refused(body) or not (creds.source_path or creds.source_keychain):
            return False
        if os.environ.get(ENV_TRUSTED_DEVICE_TOKEN):
            return False
        if self._reload_credentials():
            return True
        refused = creds.trusted_device_token
        try:
            stored, may_enroll = _stored_device_token(creds)
        except CredentialsError:
            return False
        if stored and stored != refused:
            creds.trusted_device_token = stored
        elif may_enroll and self._persist_refresh and _may_try_enrolling():
            enroll_trusted_device(creds, base_url=self.base_url, client=self._refresher, after_refusal=True)
        return bool(creds.trusted_device_token) and creds.trusted_device_token != refused

    # -- low-level request: one 401 -> refresh retry, one 403 -> device retry ----
    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        extra = kw.pop("extra_headers", None)
        resp = self._http.request(method, url, headers=self._headers(extra), **kw)
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
                resp = self._http.request(method, url, headers=self._headers(extra), **kw)
        if resp.status_code == 403 and self._renew_device_token(resp.text):
            # The org requires Trusted Devices: retry once with the token the
            # store gained or this machine just enrolled.
            resp = self._http.request(method, url, headers=self._headers(extra), **kw)
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
    def list_sessions(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        statuses: str | Iterable[str] | None = None,
        tags: str | Iterable[str] | None = None,
        exclude_tags: str | Iterable[str] | None = None,
    ) -> dict:
        """``GET /v1/code/sessions`` — your sessions, newest first:
        ``{data, next_cursor, resume_token}``. Pass ``next_cursor`` back as
        ``cursor`` for the next page; ``resume_token`` is where
        :meth:`watch_sessions` picks up from this page.

        A page holds ``limit`` sessions (20 unless given, 200 at most), archived
        ones included. Narrow it with ``statuses`` (``active`` | ``archived``),
        ``tags`` (sessions carrying any of them, e.g. :data:`PINNED_TAG`) and
        ``exclude_tags``; each takes one value or several."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        for name, value in (("statuses", statuses), ("tags", tags), ("exclude_tags", exclude_tags)):
            if value is not None:
                params[name] = [value] if isinstance(value, str) else list(value)
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
        """``GET /v1/code/sessions/{id}`` — the session object (unwrapped from the
        ``response_shape`` envelope the endpoint returns)."""
        return _unwrap_session(self._ok(self._request("GET", self._url(session_id))))

    def update_session(self, session_id: str, **fields: Any) -> dict:
        """``PUT /v1/code/sessions/{id}`` — change ``title``, or ``add_tags`` /
        ``remove_tags``. Returns the updated session."""
        return _unwrap_session(self._ok(self._request("PUT", self._url(session_id), json=fields)))

    def rename_session(self, session_id: str, title: str) -> dict:
        """Retitle a session where every controller's list shows it (a connected
        worker adopts the new title too)."""
        return self.update_session(session_id, title=title)

    def pin_session(self, session_id: str) -> dict:
        """Pin a session in claude.ai/code's sidebar (the :data:`PINNED_TAG` tag).
        Returns the updated session; ``list_sessions(tags=PINNED_TAG)`` lists pins."""
        return self.update_session(session_id, add_tags=[PINNED_TAG])

    def unpin_session(self, session_id: str) -> dict:
        """Undo :meth:`pin_session`."""
        return self.update_session(session_id, remove_tags=[PINNED_TAG])

    def archive_session(self, session_id: str) -> bool:
        """``POST /v1/code/sessions/{id}/archive`` — end/hide a session. 409 = already archived."""
        return self._transition(session_id, "archive")

    def unarchive_session(self, session_id: str) -> bool:
        """``POST /v1/code/sessions/{id}/unarchive`` — bring an archived session
        back to ``active``. 409 = it wasn't archived."""
        return self._transition(session_id, "unarchive")

    def _transition(self, session_id: str, action: str) -> bool:
        resp = self._request("POST", self._url(session_id, action), json={})
        if resp.status_code in (200, 409):
            return True
        self._ok(resp)  # raises
        return False

    def watch_sessions(
        self,
        *,
        resume_token: str | None = None,
        reconnect: bool = True,
        max_reconnects: int = 20,
    ) -> Iterator[tuple[str, dict]]:
        """``GET /v1/code/sessions/watch`` — live changes to your session list,
        as ``(kind, session)``. ``kind`` is ``added`` the first time the watch
        shows a session (a new one, or its first change since the watch began)
        and ``changed`` after that: its status, title, tags or metadata moved.
        ``session`` is the whole session object, as in a list page.

        The watch runs from a ``resume_token``. Pass the one a
        :meth:`list_sessions` page carried to follow on from that page with
        nothing missed; without one it starts from now. The server issues a
        newer token as it goes, and a reconnect resumes from that. A token too
        old to resume from ends the watch with :class:`APIError` 410: list
        again and start over.
        """
        token = resume_token or self.list_sessions(limit=1).get("resume_token")

        def resume() -> tuple[dict, dict]:
            return {}, ({"Last-Event-ID": token} if token else {})

        url = self._url("watch")
        for frame in self._sse_frames(url, resume, reconnect=reconnect, max_reconnects=max_reconnects):
            if frame.event == "sync":  # caught up to here: the next resume point
                token = frame.id or token
            elif frame.event in ("added", "changed"):
                try:
                    session = json.loads(frame.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(session, dict):
                    yield frame.event, session

    # -- sending events (steering) ----------------------------------------
    def send_events(self, session_id: str, events: Iterable[dict]) -> dict:
        """``POST /v1/code/sessions/{id}/events``.

        Body is ``{session_id, events:[{payload: <event+uuid>}]}`` — each event is
        wrapped in ``payload`` and given a ``uuid`` if it lacks one (matching the CLI).
        The ack is ``{results: [{event_id, sequence_num, duplicate}], attestation_feedback}``.
        """
        wrapped = []
        for e in events:
            if "uuid" not in e:
                e = {**e, "uuid": str(uuid.uuid4())}
            wrapped.append({"payload": e})
        body = {"session_id": session_id, "events": wrapped}
        return self._ok(self._request("POST", self._url(session_id, "events"), json=body), 200, 201)

    def send_message(self, session_id: str, text: str, *, message_uuid: str | None = None) -> dict:
        """Send a user message (Claude Code stream-json ``user`` event) — what you
        type at claude.ai/code. Pass ``message_uuid`` to keep a handle for
        :meth:`cancel_queued_message`."""
        return self.send_events(session_id, [cli_user_message(text, message_uuid=message_uuid)])

    def send_raw(self, session_id: str, payload: dict) -> dict:
        """Send an arbitrary stream-json payload (advanced escape hatch)."""
        return self.send_events(session_id, [payload])

    def cancel_queued_message(self, session_id: str, message_uuid: str, *, timeout: float = 15.0) -> bool:
        """Withdraw a message the worker hasn't started on yet — one sent with
        ``message_uuid`` that is queued behind a running turn or prompt. True if
        it was removed; False once a turn has picked it up."""
        response = self.request_control(session_id, "cancel_async_message", message_uuid=message_uuid, timeout=timeout)
        return bool(response.get("cancelled"))

    # -- answering the worker's blocking prompts ----------------------------
    def respond_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        allow: bool,
        tool_input: dict | None = None,
        tool_use_id: str | None = None,
        message: str | None = None,
    ) -> dict:
        """Allow or deny a ``can_use_tool`` prompt (``Event.is_blocking_control``).
        See :func:`~claude_rc.events.cli_permission_response` for the fields."""
        answer = cli_permission_response(
            request_id, allow=allow, tool_input=tool_input, tool_use_id=tool_use_id, message=message
        )
        return self.send_events(session_id, [answer])

    def answer_question(
        self,
        session_id: str,
        request_id: str,
        answers: dict[str, str | list[str]],
        *,
        tool_input: dict,
        tool_use_id: str | None = None,
    ) -> dict:
        """Answer an ``AskUserQuestion`` prompt: ``answers`` maps question text to
        the chosen label(s); ``{}`` dismisses it. See
        :func:`~claude_rc.events.cli_question_response`."""
        answer = cli_question_response(request_id, answers, tool_input=tool_input, tool_use_id=tool_use_id)
        return self.send_events(session_id, [answer])

    # -- the control protocol -----------------------------------------------
    def _send_control(self, session_id: str, subtype: str, **fields: Any) -> tuple[str, dict]:
        request_id = f"{subtype}-{uuid.uuid4()}"
        ack = self.send_events(session_id, [cli_control_request(subtype, request_id=request_id, **fields)])
        return request_id, ack

    def send_control(self, session_id: str, subtype: str, **fields: Any) -> str:
        """Send a ``control_request`` without waiting; returns its request id
        (pair with :meth:`wait_control_response`)."""
        return self._send_control(session_id, subtype, **fields)[0]

    def _control(self, session_id: str, subtype: str, timeout: float, **fields: Any) -> tuple[str, dict]:
        """Send a control and wait for its verdict: ``(request_id, verdict)``."""
        request_id, ack = self._send_control(session_id, subtype, **fields)
        verdict = self.wait_control_response(
            session_id, request_id, timeout=timeout, after_sequence_num=_ack_sequence(ack)
        )
        if verdict is None:
            raise ControlTimeout(request_id, f"no answer to {subtype} within {timeout:g}s")
        if verdict.get("subtype") == "error":
            raise ControlRejected(str(verdict.get("error") or f"{subtype} rejected"), verdict.get("error_code"))
        return request_id, verdict

    def request_control(self, session_id: str, subtype: str, *, timeout: float = 15.0, **fields: Any) -> dict:
        """Send a ``control_request`` and wait for the worker's verdict.

        Returns the verdict's ``response`` payload (``{}`` when it has none).
        Raises :class:`ControlRejected` on an error verdict and
        :class:`ControlTimeout` when none arrives within ``timeout``. The typed
        methods below wrap the subtypes worth having; this reaches the rest
        (``read_file``, ``file_suggestions``, ``mcp_status`` …, see
        API_REFERENCE.md).
        """
        return self._control(session_id, subtype, timeout, **fields)[1].get("response") or {}

    def wait_control_response(
        self,
        session_id: str,
        request_id: str,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.4,
        after_sequence_num: int | None = None,
    ) -> dict | None:
        """Poll history for the worker's ``control_response`` to ``request_id``.

        Returns the ``response`` object (``subtype`` is ``success`` | ``error``),
        or ``None`` if none arrived within ``timeout``. Controls are acked
        asynchronously through the event stream — a 200 on the ingest POST only
        means "queued", so this is how a caller learns whether the worker
        actually applied one. ``after_sequence_num`` (the request's own, from
        the send ack) makes the scan exact; without it each poll checks the
        newest events. The interval backs off for slow verdicts.
        """
        deadline = time.monotonic() + timeout
        delay = poll_interval
        cursor = after_sequence_num
        while True:
            if cursor is None:
                page = self.list_events(session_id, limit=_VERDICT_SCAN, sort_order="desc")
            else:
                page = self.list_events(session_id, limit=_VERDICT_SCAN, sort_order="asc", cursor=str(cursor))
            for ev in page:
                # Only the worker answers a controller's request; a controller's
                # own `control_response` is its answer to a worker prompt.
                if ev.type == RC.CONTROL_RESPONSE and ev.request_id == request_id and ev.source != "client":
                    return ev.payload.get("response") or {}
            if cursor is not None:
                cursor = max([cursor, *(ev.sequence_num or 0 for ev in page)])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if cursor is not None and len(page) == _VERDICT_SCAN:
                continue  # a full page: more history is already waiting
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, _MAX_POLL_INTERVAL)

    def interrupt(self, session_id: str) -> dict:
        """Interrupt the running agent via a ``control_request`` (jumps the queue)."""
        req = cli_control_request("interrupt", request_id=f"interrupt-{uuid.uuid4()}")
        return self.send_events(session_id, [req])

    def set_model(self, session_id: str, model: str) -> dict:
        """Change the session's model (e.g. ``claude-opus-5-5``)."""
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
        """Set reasoning effort: ``low`` | ``medium`` | ``high`` | ``xhigh`` | ``max``;
        ``None`` = auto (the model's default).

        There is no ``set_effort`` control subtype — the CLI's ``/effort`` command
        sends ``apply_flag_settings`` with ``settings.effortLevel`` (an explicit
        ``null`` clears back to auto) and ``ultracode`` alongside it (a plain
        effort change switches ultracode off; ``ultracode=True`` needs ``xhigh``
        and an xhigh-capable model). Applied this way the level is
        session-scoped: the worker re-emits ``system/init`` with the new
        ``effort`` and persists nothing.

        Remote-control workers refused ``apply_flag_settings`` through at least
        2.1.212 (``"REPL bridge does not handle…"``); 2.1.301 applies it. For
        older workers, two opt-ins:

        * ``wait`` (seconds) — poll for the worker's ``control_response`` and act
          on the verdict instead of firing blind. Without it, a refusal only ever
          surfaces in the event stream.
        * ``command_fallback`` — on refusal, inject ``/effort <level>`` as a user
          message instead. Workers run slash commands from controllers as local
          commands (zero cost, no model turn) — but with CLI semantics: a plain
          level is also *saved as that machine's default for the model*, not
          session-scoped. Requires ``wait``. Without it a refusal raises
          :class:`ControlRejected`.

        Returns ``{"via": "control" | "command" | "control_unconfirmed", "ack": …}``
        — ``control_unconfirmed`` means ``wait`` elapsed with no verdict (assumed
        applied; no fallback is attempted, to avoid a double apply).
        """
        request_id, ack = self._send_control(
            session_id, "apply_flag_settings", settings={"effortLevel": effort, "ultracode": ultracode}
        )
        if wait is None:
            return {"via": "control", "ack": ack}
        resp = self.wait_control_response(
            session_id, request_id, timeout=wait, after_sequence_num=_ack_sequence(ack)
        )
        if resp is None:
            return {"via": "control_unconfirmed", "ack": ack}
        if resp.get("subtype") != "error":
            return {"via": "control", "ack": ack}
        if not command_fallback:
            raise ControlRejected(str(resp.get("error") or "control request rejected"), resp.get("error_code"))
        command = "ultracode" if ultracode else (effort or "auto")
        ack = self.send_message(session_id, f"/effort {command}")
        return {"via": "command", "ack": ack}

    # -- asking the worker --------------------------------------------------
    def initialize(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The controller handshake claude.ai sends on every attach. Returns the
        worker's ``claude_code_version``, ``current_model``,
        ``current_permission_mode`` … plus ``pending_permission_requests`` /
        ``pending_user_dialog_requests`` (the prompts it is blocked on). Like an
        attach, it makes the worker post a fresh ``system/init`` event."""
        verdict = self._control(session_id, "initialize", timeout)[1]
        return {
            **(verdict.get("response") or {}),
            "pending_permission_requests": verdict.get("pending_permission_requests") or [],
            "pending_user_dialog_requests": verdict.get("pending_user_dialog_requests") or [],
        }

    def side_question(
        self,
        session_id: str,
        question: str,
        *,
        history: list[dict] | None = None,
        timeout: float = 300.0,
    ) -> str | None:
        """Ask the session something on the side — claude.ai's "btw". The worker
        answers from its conversation without interrupting a running turn or
        adding to the transcript (it does cost a model call).

        ``history`` holds this side thread's earlier ``{question, response}``
        pairs, so follow-ups make sense. Returns the answer, or None when the
        worker has nothing to answer from (e.g. an empty conversation). On
        ``timeout`` the question is withdrawn and :class:`ControlTimeout` raised.
        """
        fields: dict[str, Any] = {"question": question}
        if history:
            fields["history"] = [{"question": h["question"], "response": h["response"]} for h in history]
        try:
            verdict = self._control(session_id, "side_question", timeout, **fields)[1]
        except ControlTimeout as exc:
            try:  # don't leave the worker answering a question nobody waits for
                self.send_events(session_id, [cli_control_cancel_request(exc.request_id)])
            except APIError:
                pass
            raise
        return (verdict.get("response") or {}).get("response")

    def get_context_usage(self, session_id: str, *, detail: str = "summary", timeout: float = 15.0) -> dict:
        """How full the context window is: ``totalTokens`` / ``maxTokens`` /
        ``percentage``, ``autoCompactThreshold``, and a per-``categories``
        breakdown. The headline numbers also ride the session object
        (``external_metadata.context_usage``) with no round trip."""
        return self.request_control(session_id, "get_context_usage", detail=detail, timeout=timeout)

    def get_usage(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Plan usage as the worker's account sees it: ``rate_limits.limits`` (the
        5-hour and weekly windows, each ``{kind, percent, resets_at}``), the
        ``session``'s cost and line totals, and ``subscription_type``."""
        return self.request_control(session_id, "get_usage", timeout=timeout)

    def get_workspace_diff(self, session_id: str, *, timeout: float = 15.0) -> dict | None:
        """The worker's uncommitted git changes — ``stats`` (``filesCount``,
        ``linesAdded``, ``linesRemoved``), ``perFileStats``, ``hunks`` — or None
        outside a git repo. A diff still computing after ~8 s is refused with
        ``workspace_diff_pending`` (raised as :class:`ControlRejected`)."""
        return self.request_control(session_id, "get_workspace_diff", timeout=timeout).get("diff")

    def stop_task(self, session_id: str, task_id: str, *, timeout: float = 15.0) -> None:
        """Stop a running shell/agent task by the ``task_id`` of its
        ``system/task_started`` event (``system/background_tasks_changed`` lists
        the running background set). The worker acks unknown ids too; a stopped
        task reports ``task_notification`` with ``status: stopped``."""
        self.request_control(session_id, "stop_task", task_id=task_id, timeout=timeout)

    def mark_read(self, session_id: str, event_id: str | None = None) -> Any:
        """``POST /v1/code/sessions/{id}/mark_read`` — clear the session's
        ``unread`` flag, optionally naming the last event read (its
        ``Event.event_id``). Returns ``{"unread": false}``."""
        body = {"event_id": event_id} if event_id else {}
        return self._ok(self._request("POST", self._url(session_id, "mark_read"), json=body), 200, 204)

    # -- reading events: polling ------------------------------------------
    def list_events(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        sort_order: str | None = None,
        cursor: str | None = None,
    ) -> list[Event]:
        """``GET /v1/code/sessions/{id}/events`` — paginated history (returns immediately).

        ``sort_order`` is ``asc`` (oldest first — the session's FIRST events) or
        ``desc`` (newest first, what the CLI fetches). ``cursor`` is a sequence
        number: ``asc`` returns the events after it, ``desc`` the events before
        it (each response's ``next_cursor`` is the last one it held).
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if sort_order is not None:
            params["sort_order"] = sort_order
        if cursor is not None:
            params["cursor"] = cursor
        body = self._ok(self._request("GET", self._url(session_id, "events"), params=params))
        rows = body if isinstance(body, list) else body.get("data") or body.get("events") or []
        return [Event.from_wire(r) for r in rows]

    # -- reading events: streaming (SSE) ----------------------------------
    def _sse_frames(
        self,
        url: str,
        resume: Callable[[], tuple[dict, dict]],
        *,
        reconnect: bool,
        max_reconnects: int,
    ) -> Iterator[SSEFrame]:
        """The frames of one of the API's SSE streams, redialled as needed.

        ``resume()`` gives each dial's query params and extra headers, so a
        redial picks up from wherever the caller has read to. A dropped or
        silent connection redials with exponential backoff (``reconnect``),
        and so does one the server closes. A connect refused for want of a
        trusted device redials at once, ``reconnect`` or not, when a new device
        token is in hand; any other refusal raises :class:`APIError`.
        """
        attempts = 0
        while True:
            params, extra = resume()
            headers = self._headers({"Accept": "text/event-stream", **extra})
            try:
                with self._http.stream(
                    "GET", url, headers=headers, params=params,
                    timeout=httpx.Timeout(30.0, read=_STREAM_SILENCE_S),
                ) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode(errors="replace")
                        if resp.status_code == 403 and self._renew_device_token(body):
                            continue  # redial with the new trusted-device token
                        raise APIError(resp.status_code, body, resp.headers.get("request-id"))
                    attempts = 0
                    for frame in parse_sse(resp.iter_lines()):
                        if not frame.is_heartbeat:
                            yield frame
            except httpx.HTTPError:
                if not reconnect or attempts >= max_reconnects:
                    raise
            if not reconnect:
                return
            attempts += 1
            _backoff_sleep(attempts)

    def stream_events(
        self,
        session_id: str,
        *,
        from_sequence_num: int = 0,
        reconnect: bool = True,
        max_reconnects: int = 20,
        control_only: bool = False,
    ) -> Iterator[Event]:
        """``GET /v1/code/sessions/{id}/events/stream`` — live SSE events.

        Replays every persisted event after ``from_sequence_num`` (exclusive — 0
        replays the whole backlog), then follows live. A ``client_event`` frame is
        an envelope ``{event_type, sequence_num, source, payload}``; this unwraps
        ``payload`` into an :class:`Event`. ``ephemeral_event`` frames (partial
        ``stream_event`` deltas, ``system/thinking_tokens``) come through with no
        sequence number, and a ``catch_up_truncated`` frame (a gap in the replay)
        as an event of that type. The stream's other frames (``session_update``,
        ``delivery_update``, ``security_update``) are skipped. ``control_only``
        asks the server for the control protocol alone: ``control_request`` and
        ``control_response`` events.

        Resumes with both ``?from_sequence_num=`` and a ``Last-Event-ID`` header,
        de-duping by ``sequence_num``, so a dropped connection neither replays
        nor skips events. Reconnects with exponential backoff, also when the
        stream has been silent for 45 s (it sends keepalives about every 15 s).
        A connect refused for want of a trusted device reconnects at once (even
        with ``reconnect=False``) when a new device token is in hand.
        """
        seen: set[int] = set()
        last_seq = from_sequence_num

        def resume() -> tuple[dict, dict]:
            params: dict[str, Any] = {"from_sequence_num": last_seq}
            if control_only:
                params["control_only"] = "1"
            return params, ({"Last-Event-ID": str(last_seq)} if last_seq else {})

        url = self._url(session_id, "events", "stream")
        for frame in self._sse_frames(url, resume, reconnect=reconnect, max_reconnects=max_reconnects):
            # The RC stream marks each frame's sequence_num on the id: line.
            if frame.id:
                try:
                    last_seq = max(last_seq, int(frame.id))
                except ValueError:
                    pass
            try:
                data = json.loads(frame.data)
            except (json.JSONDecodeError, TypeError):
                continue
            if frame.event == "catch_up_truncated":
                yield Event(payload={**(data if isinstance(data, dict) else {}), "type": RC.CATCH_UP_TRUNCATED})
                continue
            if not (isinstance(data, dict) and isinstance(data.get("payload"), dict)):
                continue  # session_update, delivery_update, security_update …
            ev = Event.from_wire(data)
            if ev.sequence_num is not None:
                if ev.sequence_num in seen:
                    continue
                seen.add(ev.sequence_num)
                last_seq = max(last_seq, ev.sequence_num)
            yield ev

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
    ) -> list[Event]:
        """Send a message and stream until the session goes idle again.

        Returns every event received during the turn. This is the "ask and wait
        for the answer" pattern.

        Nothing is lost to the gap between sending and connecting. :meth:`stream_events`
        is a generator, so the HTTP connection only opens on the first iteration
        below — *after* the send — but the stream is resumed from the newest
        sequence number that existed beforehand, so the server replays anything
        that happened in between. A dropped connection resumes the same way.

        The turn that ends is the next one to finish. A message sent while a
        turn is running usually joins that turn; when it waits for a turn of
        its own instead, this returns at the end of the running one.
        """
        # Seed the resume cursor from the newest event so we only see new ones.
        try:
            history = self.list_events(session_id, limit=1, sort_order="desc")
            start = max((e.sequence_num or 0) for e in history) if history else 0
        except APIError:
            start = 0

        collected: list[Event] = []
        stream = self.stream_events(session_id, from_sequence_num=start)
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
