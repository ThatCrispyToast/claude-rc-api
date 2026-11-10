"""Event model and constants for the sessions API.

Two wire formats share these endpoints:

* **Remote Control** (``/v1/code/sessions``) relays the Claude Code CLI's native
  *stream-json* events, each wrapped in an envelope
  ``{"sequence_num", "event_id", "source", "created_at", "device_attestation_status", "payload": {...}}``.
  Payload ``type`` values are ``user`` / ``assistant`` / ``system`` / ``result`` /
  ``stream_event`` / ``control_request`` / ``control_response`` (plus a few
  auxiliary ones). Message text lives under ``payload.message.content`` (a string
  or a list of content blocks).

* **Managed Agents** (``/v1/sessions``) uses ``{domain}.{action}`` event types
  (``agent.message``, ``session.status_idle`` …) with text under a top-level
  ``content`` list and ``id`` / ``processed_at`` metadata.

:class:`Event` handles both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _to_int(v: Any) -> Optional[int]:
    """Sequence numbers arrive as strings on the wire; normalise to int."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- Remote Control (Claude Code stream-json) event types ------------------
class RC:
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"            # subtypes: see RCSystem
    RESULT = "result"           # subtype: success | error_max_turns | error_during_execution
    STREAM_EVENT = "stream_event"  # partial streaming (message_start, content_block_delta, ...)
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"
    CONTROL_CANCEL_REQUEST = "control_cancel_request"  # withdraws a request: a controller's, or the worker's own prompt
    RATE_LIMIT_EVENT = "rate_limit_event"
    CONVERSATION_RESET = "conversation_reset"  # /clear and friends: what came before is no longer the conversation
    CATCH_UP_TRUNCATED = "catch_up_truncated"  # stream only: the replay skipped part of the history


class RCSystem:
    """``system`` event subtypes a Remote Control worker emits."""

    INIT = "init"                  # the worker's live settings (see Event.worker_state)
    STATUS = "status"              # permissionMode + status (e.g. "compacting")
    COMPACT_BOUNDARY = "compact_boundary"
    TASK_STARTED = "task_started"  # a shell/agent task got a task_id (stop_task target)
    TASK_UPDATED = "task_updated"
    TASK_NOTIFICATION = "task_notification"  # a task finished: status completed | stopped | ...
    BACKGROUND_TASKS_CHANGED = "background_tasks_changed"  # tasks: the running background set
    CONTROL_REQUEST_PROGRESS = "control_request_progress"  # e.g. a side_question working
    INFORMATIONAL = "informational"
    VCS_STATE_CHANGED = "vcs_state_changed"  # the worker's repo moved: cwd + kind (e.g. "push")
    WORKER_SHUTTING_DOWN = "worker_shutting_down"  # the Claude Code process is exiting (reason)


# --- Managed Agents event types --------------------------------------------
class Send:
    USER_MESSAGE = "user.message"
    USER_INTERRUPT = "user.interrupt"
    USER_TOOL_CONFIRMATION = "user.tool_confirmation"
    USER_CUSTOM_TOOL_RESULT = "user.custom_tool_result"
    USER_DEFINE_OUTCOME = "user.define_outcome"
    SYSTEM_MESSAGE = "system.message"


class Recv:
    AGENT_MESSAGE = "agent.message"
    AGENT_THINKING = "agent.thinking"
    AGENT_TOOL_USE = "agent.tool_use"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_MCP_TOOL_USE = "agent.mcp_tool_use"
    AGENT_MCP_TOOL_RESULT = "agent.mcp_tool_result"
    AGENT_CUSTOM_TOOL_USE = "agent.custom_tool_use"
    SESSION_STATUS_RUNNING = "session.status_running"
    SESSION_STATUS_IDLE = "session.status_idle"
    SESSION_STATUS_TERMINATED = "session.status_terminated"
    SESSION_ERROR = "session.error"
    SPAN_MODEL_REQUEST_START = "span.model_request_start"
    SPAN_MODEL_REQUEST_END = "span.model_request_end"
    EVENT_START = "event_start"
    EVENT_DELTA = "event_delta"


# Turn-completion markers.
RC_TURN_END = {RC.RESULT}
MA_IDLE = {Recv.SESSION_STATUS_IDLE}
MA_TERMINAL = {Recv.SESSION_STATUS_TERMINATED}

# control_request subtypes a WORKER sends to block its turn until a controller
# answers. (`side_question` was once listed here: it runs the other way, a
# controller asking the worker — see RemoteControlClient.side_question.)
BLOCKING_CONTROL_SUBTYPES = {"can_use_tool", "request_user_dialog"}

# system/init + system/status keys that describe the worker's live settings.
_WORKER_STATE_KEYS = {
    "model": "model",
    "effort": "effort",
    "permissionMode": "permission_mode",
    "claude_code_version": "claude_code_version",
}


@dataclass
class Event:
    """A single event, unwrapped from its transport envelope."""

    payload: dict[str, Any]
    sequence_num: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    # -- common accessors --------------------------------------------------
    @property
    def type(self) -> Optional[str]:
        return self.payload.get("type")

    @property
    def subtype(self) -> Optional[str]:
        return self.payload.get("subtype")

    @property
    def id(self) -> Optional[str]:
        return self.payload.get("id") or self.payload.get("uuid")

    @property
    def processed_at(self) -> Optional[str]:
        return self.payload.get("processed_at") or self.payload.get("timestamp")

    @property
    def role(self) -> Optional[str]:
        msg = self.payload.get("message")
        if isinstance(msg, dict):
            return msg.get("role")
        return None

    @property
    def stop_reason(self) -> Any:
        return self.payload.get("stop_reason")

    # -- Remote Control envelope -------------------------------------------
    @property
    def event_id(self) -> Optional[str]:
        return self.raw.get("event_id")

    @property
    def source(self) -> Optional[str]:
        """Who wrote the event: ``"client"`` (a controller: this library,
        claude.ai, the mobile app) or ``"worker"`` (the Claude Code process)."""
        return self.raw.get("source")

    @property
    def created_at(self) -> Optional[str]:
        return self.raw.get("created_at")

    @property
    def attestation(self) -> Optional[str]:
        """``device_attestation_status`` without its enum prefix: ``UNSPECIFIED``,
        ``ABSENT``, ``VERIFIED``, ``INVALID`` … (None when absent). A worker whose
        org enforces Trusted Devices drops unverified controller events."""
        status = self.raw.get("device_attestation_status")
        if not isinstance(status, str):
            return None
        return status.removeprefix("DEVICE_ATTESTATION_STATUS_")

    # -- content -------------------------------------------------------------
    def _content_blocks(self) -> list:
        """Return content blocks regardless of wire format."""
        msg = self.payload.get("message")
        if isinstance(msg, dict):  # Remote Control (CLI stream-json)
            content = msg.get("content")
        else:  # Managed Agents
            content = self.payload.get("content")
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return content or []

    def text(self) -> str:
        """All text from the event's content blocks joined together."""
        out = []
        for block in self._content_blocks():
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "".join(out)

    def tool_uses(self) -> list[dict]:
        """``tool_use`` content blocks (name, input, id)."""
        return [b for b in self._content_blocks() if isinstance(b, dict) and b.get("type") == "tool_use"]

    # -- control protocol ----------------------------------------------------
    @property
    def request_id(self) -> Optional[str]:
        """The id a ``control_request`` carries, the one a ``control_response``
        answers, or the one a ``control_cancel_request`` withdraws; None for
        every other event."""
        if self.type in (RC.CONTROL_REQUEST, RC.CONTROL_CANCEL_REQUEST):
            return self.payload.get("request_id")
        if self.type == RC.CONTROL_RESPONSE:
            return (self.payload.get("response") or {}).get("request_id") or self.payload.get("request_id")
        return None

    @property
    def control_subtype(self) -> Optional[str]:
        """``request.subtype`` of a ``control_request`` (``can_use_tool``, ``interrupt`` …)."""
        if self.type != RC.CONTROL_REQUEST:
            return None
        return (self.payload.get("request") or {}).get("subtype")

    @property
    def worker_state(self) -> dict[str, Any]:
        """The worker settings a ``system/init`` or ``system/status`` event reports,
        as ``{model, effort, permission_mode, claude_code_version}`` (only the keys
        it carries; ``{}`` for any other event). Workers re-emit ``init`` whenever
        the model, permission mode or effort changes and on every controller
        attach, so the newest one is the live state."""
        if self.type != RC.SYSTEM or self.subtype not in (RCSystem.INIT, RCSystem.STATUS):
            return {}
        return {
            name: self.payload[key]
            for key, name in _WORKER_STATE_KEYS.items()
            if self.payload.get(key) is not None
        }

    # -- turn/idle detection (RC + MA) ------------------------------------
    @property
    def is_turn_end(self) -> bool:
        return self.type in RC_TURN_END or self.type in MA_IDLE

    @property
    def is_terminal(self) -> bool:
        return self.type in MA_TERMINAL or (self.type == RC.RESULT and self.subtype != "success")

    @property
    def is_blocking_control(self) -> bool:
        """A worker prompt (tool permission or dialog) that blocks its turn until a
        controller answers. Requests a controller sent — your own echoing back, or
        claude.ai's — never block anything."""
        return self.control_subtype in BLOCKING_CONTROL_SUBTYPES and self.source != "client"

    @property
    def settles_prompt(self) -> bool:
        """This event ends a blocking prompt, the one its ``request_id`` names:
        a controller's answer (any controller's, echoed to all), or the worker
        withdrawing the prompt because it was answered at the terminal or its
        turn was interrupted."""
        if self.type == RC.CONTROL_RESPONSE:
            return self.source != "worker"  # a worker's is its verdict on a controller's request
        return self.type == RC.CONTROL_CANCEL_REQUEST and self.source != "client"

    @classmethod
    def from_wire(cls, obj: dict[str, Any]) -> "Event":
        if isinstance(obj, dict) and isinstance(obj.get("payload"), dict):
            return cls(payload=obj["payload"], sequence_num=_to_int(obj.get("sequence_num")), raw=obj)
        return cls(payload=obj, sequence_num=_to_int((obj or {}).get("sequence_num")), raw=obj or {})

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        seq = f" seq={self.sequence_num}" if self.sequence_num is not None else ""
        sub = f"/{self.subtype}" if self.subtype else ""
        preview = self.text()[:60].replace("\n", " ")
        extra = f" {preview!r}" if preview else ""
        return f"<Event {self.type}{sub}{seq}{extra}>"


# --- Outbound builders: Remote Control (CLI stream-json) -------------------
def cli_user_message(
    text: str, *, parent_tool_use_id: str | None = None, message_uuid: str | None = None
) -> dict:
    """A Remote Control user message (Claude Code stream-json format).

    ``message_uuid`` pins the event's ``uuid`` (otherwise ``send_events`` mints
    one) — keep it to withdraw the message while it is still queued
    (:meth:`~claude_rc.client.RemoteControlClient.cancel_queued_message`).
    """
    msg: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": parent_tool_use_id,
    }
    if message_uuid:
        msg["uuid"] = message_uuid
    return msg


def cli_control_request(subtype: str, request_id: str, **fields: Any) -> dict:
    """A Remote Control control request (e.g. interrupt, set_model)."""
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": subtype, **fields},
    }


def cli_control_cancel_request(request_id: str) -> dict:
    """Withdraw an in-flight control request (e.g. a slow ``side_question``); the
    worker then answers it with an error ``control_response``."""
    return {"type": "control_cancel_request", "request_id": request_id}


def cli_control_response(request_id: str, response: dict, *, tool_use_id: str | None = None) -> dict:
    """A controller's answer to a worker's blocking ``control_request``: outer
    ``subtype: success`` (the request was handled) around the actual decision."""
    resp: dict[str, Any] = {"subtype": "success", "request_id": request_id, "response": response}
    if tool_use_id:
        resp["tool_use_id"] = tool_use_id
    return {"type": "control_response", "response": resp}


def cli_permission_response(
    request_id: str,
    *,
    allow: bool,
    tool_input: dict | None = None,
    tool_use_id: str | None = None,
    message: str | None = None,
) -> dict:
    """Answer a ``can_use_tool`` permission prompt.

    An allow may carry ``tool_input`` as ``updatedInput`` — the tool then runs
    with it instead of what it asked for. Pass the request's own ``input`` (edited
    or not): ``updatedInput`` is optional to 2.1.301 workers, but older ones
    expect it. A deny's ``message`` is what the model is told.
    """
    if allow:
        decision: dict[str, Any] = {"behavior": "allow"}
        if tool_input is not None:
            decision["updatedInput"] = tool_input
    else:
        decision = {"behavior": "deny", "message": message or "Denied by controller"}
    return cli_control_response(request_id, decision, tool_use_id=tool_use_id)


def cli_question_response(
    request_id: str,
    answers: dict[str, str | list[str]],
    *,
    tool_input: dict,
    tool_use_id: str | None = None,
) -> dict:
    """Answer an ``AskUserQuestion`` prompt — it arrives as a ``can_use_tool``
    permission, not a dialog, so the answer is an allow whose ``updatedInput``
    is the tool's own input plus ``answers``.

    ``answers`` maps each question's TEXT to the chosen label (a list for a
    multi-select question). An empty map is the graceful dismiss ("The user did
    not answer the questions"). ``tool_input`` must keep the original
    ``questions`` — the tool crashes without them.
    """
    updated = {**tool_input, "answers": dict(answers)}
    return cli_permission_response(request_id, allow=True, tool_input=updated, tool_use_id=tool_use_id)


def cli_dialog_response(request_id: str, *, completed: bool, result: dict | None = None) -> dict:
    """Answer a true ``request_user_dialog`` prompt (elicitation, MCP approval…)
    with ``{status: completed | cancelled, result}``. UNCONFIRMED against a live
    sample — the ``result`` shape is opaque per ``dialog_kind``."""
    inner = {"status": "completed" if completed else "cancelled", "result": (result or {}) if completed else {}}
    return cli_control_response(request_id, inner)


# --- Outbound builders: Managed Agents -------------------------------------
def user_message(text: str) -> dict:
    """A Managed Agents ``user.message`` event."""
    return {"type": Send.USER_MESSAGE, "content": [{"type": "text", "text": text}]}


def interrupt() -> dict:
    """A Managed Agents interrupt event."""
    return {"type": Send.USER_INTERRUPT}


def custom_tool_result(custom_tool_use_id: str, text: str, is_error: bool = False) -> dict:
    return {
        "type": Send.USER_CUSTOM_TOOL_RESULT,
        "custom_tool_use_id": custom_tool_use_id,
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
    }
