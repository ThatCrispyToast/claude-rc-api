"""Event model and constants for the sessions API.

Two wire formats share these endpoints:

* **Remote Control** (``/v1/code/sessions``) relays the Claude Code CLI's native
  *stream-json* events, each wrapped as ``{"sequence_num": int, "payload": {...}}``.
  Payload ``type`` values are ``user`` / ``assistant`` / ``system`` / ``result`` /
  ``stream_event`` / ``control_request`` / ``control_response``. Message text lives
  under ``payload.message.content`` (a string or a list of content blocks).

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
    SYSTEM = "system"            # subtype: init | compact_boundary
    RESULT = "result"           # subtype: success | error_max_turns | error_during_execution
    STREAM_EVENT = "stream_event"  # partial streaming (message_start, content_block_delta, ...)
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"


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

# control_request subtypes that block the turn until the controller answers.
BLOCKING_CONTROL_SUBTYPES = {"can_use_tool", "request_user_dialog", "side_question"}


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

    # -- turn/idle detection (RC + MA) ------------------------------------
    @property
    def is_turn_end(self) -> bool:
        return self.type in RC_TURN_END or self.type in MA_IDLE

    @property
    def is_terminal(self) -> bool:
        return self.type in MA_TERMINAL or (self.type == RC.RESULT and self.subtype != "success")

    @property
    def is_blocking_control(self) -> bool:
        return self.type == RC.CONTROL_REQUEST and (
            (self.payload.get("request") or {}).get("subtype") in BLOCKING_CONTROL_SUBTYPES
        )

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
def cli_user_message(text: str, *, parent_tool_use_id: str | None = None) -> dict:
    """A Remote Control user message (Claude Code stream-json format)."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": parent_tool_use_id,
    }


def cli_control_request(subtype: str, request_id: str, **fields: Any) -> dict:
    """A Remote Control control request (e.g. interrupt, set_model)."""
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": subtype, **fields},
    }


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
