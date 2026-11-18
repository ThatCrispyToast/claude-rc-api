"""The worker side of a Remote Control session.

:class:`~claude_rc.client.RemoteControlClient` plays claude.ai/code: it steers a
session someone else runs. :class:`RemoteControlWorker` plays the other end, the
part the local ``claude`` process plays. It holds a session's worker seat,
receives what controllers send, and writes the transcript they show. What
answers is up to the caller, so a session can front any program: the server
checks that the worker holds the seat, not that it is Claude Code.

A minimal worker::

    rc = RemoteControlClient()
    session = rc.create_session("echo")
    with RemoteControlWorker(rc, session["id"]) as worker:
        worker.send(worker_init(model="echo", cwd=os.getcwd()))
        for ev in worker.events():
            if ev.type == "user":
                worker.begin_turn(ev)
                worker.send(worker_assistant(ev.text(), model="echo"))
                worker.end_turn(ev)
            elif ev.control_subtype == "initialize":
                worker.respond(ev, worker_initialize_response(model="echo"))
            elif ev.type == "control_request":
                worker.reject(ev, f"not supported: {ev.control_subtype}")

The session shows in the account's own claude.ai/code and Claude apps, and
nowhere else. Read from Claude Code 2.1.327 and exercised against the live API
on 2026-11-18; ``API_REFERENCE.md`` §5 has the protocol.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Iterator

import httpx

from .client import ANTHROPIC_VERSION, USER_AGENT, APIError, RemoteControlClient, _STREAM_SILENCE_S
from .credentials import CredentialsError
from .events import Event, worker_control_response, worker_permission_request, worker_result
from .sse import parse_sse

log = logging.getLogger(__name__)

# How long before its token expires a worker claims the next one.
_RENEW_MARGIN_S = 300.0
# Until the server names an interval in its first heartbeat answer.
_HEARTBEAT_S = 20.0
# How many times one worker call is sent before its failure is the caller's.
_CALL_TRIES = 4
# The pending-action fields PUT /worker takes as ``requires_action_details``.
_ACTION_DETAIL_KEYS = (
    "tool_name", "display_tool_name", "action_description", "raw_command", "request_id", "tool_use_id",
)


class WorkerEvicted(RuntimeError):
    """The server stopped taking this worker's calls (409). ``reason`` is the
    server's: ``superseded_by_worker`` when another worker claimed the session
    (a session has one worker at a time), ``session_not_active`` when it was
    archived. Either way this worker is done, and :meth:`RemoteControlWorker.events`
    raises this."""

    def __init__(self, reason: str, message: str):
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class RemoteControlWorker:
    """One session's worker: the seat the Claude Code process normally holds.

    ``client`` signs the two calls that need the account's login, creating the
    session (:meth:`RemoteControlClient.create_session`) and claiming its seat.
    Everything after that runs on the seat's own token, which this class
    renews.

    The methods mirror what the app expects of a worker. A user message goes
    through :meth:`begin_turn` and :meth:`end_turn`, a controller's control
    request is answered with :meth:`respond` or :meth:`reject`, and
    ``system/init`` is posted at the start and after each ``initialize``. A
    worker that skips these leaves claude.ai waiting on a spinner.
    """

    def __init__(self, client: RemoteControlClient, session_id: str, *, timeout: float = 30.0):
        self.client = client
        self.session_id = session_id
        self.epoch: int | None = None
        self._timeout = timeout
        self._http = httpx.Client(timeout=timeout)
        # Guards the grant: token, epoch, base URL and when to renew.
        self._lock = threading.RLock()
        self._token = ""
        self._base = ""
        self._renew_at = 0.0
        self._grants = 0  # counts claims: the event stream redials when it moves
        self._status = "idle"
        self._closed = threading.Event()
        self._evicted: WorkerEvicted | None = None
        self._stream: httpx.Response | None = None

    def __enter__(self) -> "RemoteControlWorker":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the seat ------------------------------------------------------------
    def connect(self) -> "RemoteControlWorker":
        """Claim the session's worker seat and start heartbeating. The session
        turns ``connected`` and ``idle``, and a worker that held the seat
        before is evicted. Messages sent while no worker was attached arrive
        through :meth:`events`."""
        self._claim()
        self.report_state("idle")
        threading.Thread(target=self._heartbeat, name=f"rc-worker-{self.session_id}", daemon=True).start()
        return self

    def close(self, *, goodbye: bool = True) -> None:
        """Stop serving. With ``goodbye`` the session shows as disconnected at
        once; without it the server finds out as the heartbeats stop. The
        session stays listed, and a later worker can claim it again."""
        if self._closed.is_set():
            return
        if goodbye and self.epoch is not None and self._evicted is None:
            try:
                self._call("PUT", "/worker", {"connection_status": "disconnected"})
            except (httpx.HTTPError, APIError, WorkerEvicted, CredentialsError) as e:
                log.warning("worker goodbye for %s failed: %s", self.session_id, e)
        self._closed.set()
        stream = self._stream
        if stream is not None:
            stream.close()  # ends an events() loop blocked on the stream
        self._http.close()

    def _claim(self, *, renew: bool = False) -> None:
        with self._lock:
            try:
                grant = self.client.bridge_session(
                    self.session_id, expected_epoch=self.epoch if renew else None
                )
            except APIError as e:
                if e.status == 409:
                    raise self._evict(e.body) from None
                raise
            self._token = grant["worker_jwt"]
            self.epoch = int(grant["worker_epoch"])
            self._base = f"{grant['api_base_url'].rstrip('/')}/v1/code/sessions/{self.session_id}"
            self._renew_at = time.monotonic() + float(grant["expires_in"]) - _RENEW_MARGIN_S
            self._grants += 1

    def _renew(self) -> None:
        """A fresh token for the seat this worker holds. It comes with a new
        epoch, so the worker's state is reported again under that one."""
        self._claim(renew=True)
        resp = self._http.put(
            f"{self._base}/worker",
            headers=self._headers(self._token),
            json={"worker_epoch": self.epoch, "worker_status": self._status},
        )
        if resp.status_code == 409:
            raise self._evict(resp.text)
        RemoteControlClient._ok(resp)

    def _evict(self, body: str) -> WorkerEvicted:
        try:
            error = json.loads(body).get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        self._evicted = WorkerEvicted(
            error.get("reason") or error.get("type") or "conflict", error.get("message") or body[:200]
        )
        return self._evicted

    @staticmethod
    def _headers(token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        """One ``…/worker`` call as the current epoch. An expired token is
        renewed first and a 401 renews it; a dropped connection, a 429 or a
        5xx is tried again (every call is safe to repeat: events are stored
        once per ``uuid``); a 409 means the seat is gone."""
        renewed = False
        for attempt in range(1, _CALL_TRIES + 1):
            with self._lock:
                if self._evicted is not None:
                    raise self._evicted
                if time.monotonic() >= self._renew_at:
                    self._renew()
                token = self._token
                url = self._base + path
                payload = {"worker_epoch": self.epoch, **(body or {})}
            try:
                resp = self._http.request(method, url, headers=self._headers(token), json=payload)
            except httpx.TransportError:
                if attempt == _CALL_TRIES or self._closed.is_set():
                    raise
                resp = None
            if resp is not None:
                if resp.status_code == 401 and not renewed:
                    renewed = True
                    with self._lock:
                        if self._token == token:  # no other thread renewed it meanwhile
                            self._renew()
                    continue
                if not (resp.status_code == 429 or resp.status_code >= 500) or attempt == _CALL_TRIES:
                    break
            self._closed.wait(min(8.0, 0.5 * 2 ** (attempt - 1)))
        if resp.status_code == 409:
            raise self._evict(resp.text)
        return RemoteControlClient._ok(resp)

    def _heartbeat(self) -> None:
        interval = _HEARTBEAT_S
        while not self._closed.is_set():
            try:
                answer = self._call("POST", "/worker/heartbeat", {"session_id": self.session_id})
                interval = float((answer or {}).get("heartbeat_interval_seconds") or interval)
            except WorkerEvicted:
                return  # events() raises it
            except (httpx.HTTPError, APIError, CredentialsError) as e:
                if not self._closed.is_set():
                    log.warning("worker heartbeat for %s failed: %s", self.session_id, e)
            self._closed.wait(interval)

    # -- inbound: what controllers send --------------------------------------
    def events(self, *, from_sequence_num: int = 0) -> Iterator[Event]:
        """``GET …/worker/events/stream`` — the events controllers address to
        the worker, as they arrive: ``user`` messages, ``control_request``
        events (``initialize``, ``interrupt``, ``set_model`` …), and the
        ``control_response`` / ``control_cancel_request`` that settle a prompt
        of the worker's own. Each is reported ``received`` as it is yielded.

        The server keeps track of what was delivered: from the default
        ``from_sequence_num=0`` a worker gets whatever no worker has reported
        ``processed`` yet, so nothing sent while it was away is lost and
        nothing finished is repeated. Runs until :meth:`close`, redialling a
        dropped or silent stream, and raises :class:`WorkerEvicted` when the
        seat is taken or the session archived."""
        last = from_sequence_num
        attempts = 0
        renewed = False  # for the dial in hand: a second 401 in a row is not a stale token
        while not self._closed.is_set():
            with self._lock:
                if self._evicted is not None:
                    raise self._evicted
                if time.monotonic() >= self._renew_at:
                    self._renew()
                grants, token, url = self._grants, self._token, f"{self._base}/worker/events/stream"
            headers = {**self._headers(token), "Accept": "text/event-stream"}
            if last:
                headers["Last-Event-ID"] = str(last)
            try:
                with self._http.stream(
                    "GET", url, headers=headers, params={"from_sequence_num": last} if last else None,
                    timeout=httpx.Timeout(self._timeout, read=_STREAM_SILENCE_S),
                ) as resp:
                    if resp.status_code == 401 and not renewed:
                        renewed = True
                        with self._lock:
                            if self._token == token:
                                self._renew()
                        continue
                    if resp.status_code != 200:
                        body = resp.read().decode(errors="replace")
                        if resp.status_code == 409:
                            raise self._evict(body)
                        raise APIError(resp.status_code, body, resp.headers.get("request-id"))
                    attempts, renewed = 0, False
                    self._stream = resp
                    for frame in parse_sse(resp.iter_lines(), heartbeats=True):
                        if self._closed.is_set():
                            return
                        if self._evicted is not None:
                            raise self._evicted
                        if grants != self._grants:
                            break  # the token was renewed: redial as the new epoch
                        if frame.event != "client_event":
                            continue
                        try:
                            ev = Event.from_wire(json.loads(frame.data))
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if ev.sequence_num is None or ev.sequence_num <= last:
                            continue
                        last = ev.sequence_num
                        try:
                            self.report_delivery(ev, "received")
                        except (httpx.HTTPError, APIError) as e:
                            log.warning("delivery receipt for %s failed: %s", ev.event_id, e)
                        yield ev
            except (httpx.HTTPError, httpx.StreamError):
                if self._closed.is_set():
                    return
            finally:
                self._stream = None
            attempts += 1
            self._closed.wait(min(30.0, 0.5 * 2 ** (attempts - 1)))

    # -- outbound: the transcript and the worker's state ---------------------
    def send(self, *payloads: dict, ephemeral: bool = False) -> dict:
        """``POST …/worker/events`` — write events into the session: stream-json
        payloads such as the ``worker_*`` builders in :mod:`claude_rc.events`
        make. Each gets a ``uuid`` and the ``session_id`` if it lacks them.
        ``ephemeral`` events reach the controllers watching now and are not
        stored (``stream_event`` deltas). Returns the ack,
        ``{results: [{event_id, sequence_num, duplicate}], has_subscribers}``,
        whose ``has_subscribers`` says whether any controller is watching."""
        rows = []
        for p in payloads:
            row: dict[str, Any] = {"payload": {"uuid": str(uuid.uuid4()), "session_id": self.session_id, **p}}
            if ephemeral:
                row["ephemeral"] = True
            rows.append(row)
        return self._call("POST", "/worker/events", {"events": rows})

    def report_state(self, status: str, *, pending_action: dict | None = None) -> None:
        """``PUT …/worker`` — the session's ``worker_status``: ``idle``,
        ``running``, or ``requires_action`` while a prompt waits on a
        controller. ``pending_action`` describes that prompt
        (``{tool_name, action_description, request_id, tool_use_id, input}``,
        optionally ``raw_command``); any other state clears it."""
        details = None
        if pending_action:
            details = {k: pending_action[k] for k in _ACTION_DETAIL_KEYS if k in pending_action}
        self._call("PUT", "/worker", {
            "worker_status": status,
            "requires_action_details": details,
            "external_metadata": {"pending_action": pending_action},
        })
        self._status = status

    def report_metadata(self, **metadata: Any) -> None:
        """``PUT …/worker`` — set keys of the session's ``external_metadata``,
        which every controller's session list carries: ``model``,
        ``context_usage`` (``{used_tokens, max_tokens}``) … ``None`` clears a key."""
        self._call("PUT", "/worker", {"external_metadata": metadata})

    def report_delivery(self, event: Event | str, status: str) -> None:
        """``POST …/worker/events/delivery`` — where a controller's event has
        got to: ``received``, ``processing`` (a turn took it), ``processed``
        (done with it). An event never reported ``processed`` is delivered
        again to the next worker that connects."""
        event_id = event if isinstance(event, str) else event.event_id
        self._call("POST", "/worker/events/delivery", {"updates": [{"event_id": event_id, "status": status}]})

    # -- what the app expects of a worker ------------------------------------
    def begin_turn(self, *messages: Event) -> None:
        """Start a turn on these user messages: they show as picked up, and
        the session as running."""
        for ev in messages:
            self.report_delivery(ev, "processing")
        self.report_state("running")

    def end_turn(self, *messages: Event, **result: Any) -> None:
        """End the turn: post the ``result`` event controllers wait for, mark
        the messages done and the session idle. ``result`` goes to
        :func:`~claude_rc.events.worker_result` (``result``, ``is_error``,
        ``usage`` …)."""
        answered = messages[0].id if messages else None
        self.send(worker_result(user_message_uuid=answered, **result))
        for ev in messages:
            self.report_delivery(ev, "processed")
        self.report_state("idle")

    def respond(self, request: Event, response: dict | None = None, **fields: Any) -> None:
        """Answer a controller's ``control_request`` with success. ``fields``
        ride beside the response
        (:func:`~claude_rc.events.worker_control_response`)."""
        self.send(worker_control_response(request.request_id, response, **fields))
        self.report_delivery(request, "processed")

    def reject(self, request: Event, error: str, *, error_code: str | None = None) -> None:
        """Refuse a controller's ``control_request``. Every request needs one
        answer or the other: the controller waits for it."""
        self.send(worker_control_response(request.request_id, error=error, error_code=error_code))
        self.report_delivery(request, "processed")

    def request_permission(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        tool_use_id: str,
        description: str,
        request_id: str | None = None,
        raw_command: str | None = None,
    ) -> str:
        """Ask the controllers whether a tool may run: a ``can_use_tool``
        prompt, with the session waiting in ``requires_action``. Returns the
        prompt's request id. The answer arrives through :meth:`events` as a
        ``control_response`` naming it, whose
        ``payload["response"]["response"]`` is ``{behavior: allow | deny, …}``.
        Report that event ``processed`` and the state ``running`` to go on. To
        withdraw the prompt instead, :meth:`send` a
        :func:`~claude_rc.events.cli_control_cancel_request`."""
        request_id = request_id or str(uuid.uuid4())
        self.send(worker_permission_request(
            request_id, tool_name, tool_input, tool_use_id=tool_use_id, description=description
        ))
        action = {
            "tool_name": tool_name,
            "action_description": description,
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "input": tool_input,
        }
        if raw_command:
            action["raw_command"] = raw_command
        self.report_state("requires_action", pending_action=action)
        return request_id
