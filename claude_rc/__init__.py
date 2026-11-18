"""Unofficial Python client for the Claude Code Remote Control / Sessions API.

Reverse-engineered from the Claude Code CLI. Not affiliated with or supported by
Anthropic; the underlying endpoints are private and may change without notice.

Quick start (Remote Control — steer a live `claude remote-control` session):

    from claude_rc import RemoteControlClient

    rc = RemoteControlClient()                 # reads ~/.claude credentials
    for s in rc.sessions():
        print(s.get("id"), s.get("title"))

    sid = rc.sessions()[0]["id"]
    for ev in rc.send_and_collect(sid, "run the tests", print_stream=True):
        ...

`RemoteControlWorker` takes the other side: it hosts a session of your own,
which claude.ai/code and the Claude apps then list and steer (see worker.py).
"""

from ._version import __version__
from .client import (
    APIError,
    ControlRejected,
    ControlTimeout,
    ManagedAgentsClient,
    RemoteControlClient,
    CCR_BETA,
    MANAGED_AGENTS_BETA,
    PINNED_TAG,
)
from .credentials import (
    CredentialsError,
    OAuthCredentials,
    enroll_trusted_device,
    load_credentials,
    load_org_uuid,
    refresh_credentials,
)
from .events import (
    CLAUDE_CODE_VERSION,
    Event,
    RC,
    RCSystem,
    Recv,
    Send,
    cli_user_message,
    cli_control_request,
    cli_control_cancel_request,
    cli_control_response,
    cli_permission_response,
    cli_question_response,
    cli_dialog_response,
    worker_init,
    worker_initialize_response,
    worker_assistant,
    worker_stream_event,
    worker_tool_result,
    worker_result,
    worker_control_response,
    worker_permission_request,
    worker_status,
    worker_compact_boundary,
    user_message,
    interrupt,
    custom_tool_result,
)
from .sse import SSEFrame, parse_sse
from .webui import serve as serve_webui
from .worker import RemoteControlWorker, WorkerEvicted

__all__ = [
    "__version__",
    "RemoteControlClient",
    "RemoteControlWorker",
    "WorkerEvicted",
    "ManagedAgentsClient",
    "APIError",
    "ControlRejected",
    "ControlTimeout",
    "PINNED_TAG",
    "OAuthCredentials",
    "CredentialsError",
    "enroll_trusted_device",
    "load_credentials",
    "load_org_uuid",
    "refresh_credentials",
    "CLAUDE_CODE_VERSION",
    "Event",
    "RC",
    "RCSystem",
    "Recv",
    "Send",
    "cli_user_message",
    "cli_control_request",
    "cli_control_cancel_request",
    "cli_control_response",
    "cli_permission_response",
    "cli_question_response",
    "cli_dialog_response",
    "worker_init",
    "worker_initialize_response",
    "worker_assistant",
    "worker_stream_event",
    "worker_tool_result",
    "worker_result",
    "worker_control_response",
    "worker_permission_request",
    "worker_status",
    "worker_compact_boundary",
    "user_message",
    "interrupt",
    "custom_tool_result",
    "SSEFrame",
    "parse_sse",
    "serve_webui",
    "CCR_BETA",
    "MANAGED_AGENTS_BETA",
]
