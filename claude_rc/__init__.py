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
"""

from .client import (
    APIError,
    ManagedAgentsClient,
    RemoteControlClient,
    CCR_BETA,
    MANAGED_AGENTS_BETA,
)
from .credentials import (
    CredentialsError,
    OAuthCredentials,
    load_credentials,
    load_org_uuid,
    refresh_credentials,
)
from .events import (
    Event,
    RC,
    Recv,
    Send,
    cli_user_message,
    cli_control_request,
    user_message,
    interrupt,
    custom_tool_result,
)
from .sse import SSEFrame, parse_sse
from .webui import serve as serve_webui

__version__ = "0.1.0"

__all__ = [
    "RemoteControlClient",
    "ManagedAgentsClient",
    "APIError",
    "OAuthCredentials",
    "CredentialsError",
    "load_credentials",
    "load_org_uuid",
    "refresh_credentials",
    "Event",
    "RC",
    "Recv",
    "Send",
    "cli_user_message",
    "cli_control_request",
    "user_message",
    "interrupt",
    "custom_tool_result",
    "SSEFrame",
    "parse_sse",
    "serve_webui",
    "CCR_BETA",
    "MANAGED_AGENTS_BETA",
]
