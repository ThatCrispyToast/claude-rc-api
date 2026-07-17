"""Offline tests for the reverse-engineered client (no network)."""
import json
from pathlib import Path

import pytest

from claude_rc.sse import parse_sse, SSEFrame
from claude_rc.events import (
    Event,
    cli_user_message,
    cli_control_request,
    user_message,
)
from claude_rc.credentials import OAuthCredentials, load_credentials, CredentialsError


# --- SSE parser ------------------------------------------------------------
def test_sse_basic_frame():
    lines = ["event: client_event", "id: 42", 'data: {"a":1}', ""]
    frames = list(parse_sse(lines))
    assert len(frames) == 1
    assert frames[0].event == "client_event"
    assert frames[0].id == "42"
    assert json.loads(frames[0].data) == {"a": 1}


def test_sse_multiline_data_and_heartbeat():
    lines = ["data: line1", "data: line2", "", ": keep-alive", ""]
    frames = list(parse_sse(lines))
    # comment-only block yields no frame; the data block joins with newline
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2"
    assert frames[0].event is None


def test_sse_strips_single_leading_space():
    frames = list(parse_sse(["data:  two-spaces", ""]))
    assert frames[0].data == " two-spaces"  # only the first space is stripped


# --- Event model -----------------------------------------------------------
def test_event_rc_wrapped_unwraps_payload_and_int_seq():
    wire = {"sequence_num": "362", "payload": {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}}
    e = Event.from_wire(wire)
    assert e.type == "assistant"
    assert e.role == "assistant"
    assert e.sequence_num == 362 and isinstance(e.sequence_num, int)
    assert e.text() == "hi"


def test_event_rc_string_content():
    wire = {"sequence_num": 1, "payload": {"type": "user",
            "message": {"role": "user", "content": "just a string"}}}
    e = Event.from_wire(wire)
    assert e.text() == "just a string"


def test_event_managed_agents_shape():
    wire = {"type": "agent.message", "id": "sevt_1",
            "content": [{"type": "text", "text": "cloud"}], "processed_at": "2026-01-01T00:00:00Z"}
    e = Event.from_wire(wire)
    assert e.type == "agent.message"
    assert e.text() == "cloud"
    assert e.sequence_num is None


def test_event_turn_end_and_terminal():
    assert Event.from_wire({"payload": {"type": "result", "subtype": "success"}}).is_turn_end
    assert Event.from_wire({"payload": {"type": "result", "subtype": "error_max_turns"}}).is_terminal
    assert Event.from_wire({"type": "session.status_idle"}).is_turn_end


def test_event_blocking_control():
    e = Event.from_wire({"payload": {"type": "control_request",
                                     "request": {"subtype": "can_use_tool"}}})
    assert e.is_blocking_control


def test_event_tool_uses():
    e = Event.from_wire({"payload": {"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}, "id": "toolu_1"}]}}})
    tus = e.tool_uses()
    assert len(tus) == 1 and tus[0]["name"] == "Bash"


# --- builders --------------------------------------------------------------
def test_cli_user_message_shape():
    m = cli_user_message("hello")
    assert m["type"] == "user"
    assert m["message"] == {"role": "user", "content": [{"type": "text", "text": "hello"}]}


def test_cli_control_request_shape():
    r = cli_control_request("set_model", "rid-1", model="claude-opus-4-8")
    assert r["type"] == "control_request"
    assert r["request"] == {"subtype": "set_model", "model": "claude-opus-4-8"}


def test_managed_agents_user_message_shape():
    assert user_message("x") == {"type": "user.message", "content": [{"type": "text", "text": "x"}]}


# --- credentials -----------------------------------------------------------
def test_load_credentials_from_file(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a" * 108, "refreshToken": "r" * 108,
        "expiresAt": 9999999999999, "scopes": ["user:inference"],
        "subscriptionType": "max", "clientId": "cid"}}))
    c = load_credentials(p)
    assert c.access_token == "a" * 108
    assert c.client_id == "cid"
    assert not c.is_expired()


def test_load_credentials_expired(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1}}))
    assert load_credentials(p).is_expired()


def test_load_credentials_missing_block(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"somethingElse": {}}))
    with pytest.raises(CredentialsError):
        load_credentials(p)


# --- rotated-token recovery (long-lived clients) -----------------------------
def _write_creds(p: Path, token: str, refresh: str, expires_at_ms: int) -> None:
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": refresh,
        "expiresAt": expires_at_ms, "scopes": ["user:inference"],
        "subscriptionType": "max"}}))


def _client_for(p: Path):
    from claude_rc.client import RemoteControlClient
    return RemoteControlClient(credentials_path=str(p), org_uuid="org-x")


def test_token_prefers_rotated_disk_copy(tmp_path: Path, monkeypatch):
    """Expired in memory + fresh tokens on disk (another process refreshed)
    → take the disk copy; never spend our own (already-dead) refresh token."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)  # long expired
    client = _client_for(p)

    def _no_refresh(*a, **kw):
        raise AssertionError("network refresh must not run when disk has fresh tokens")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _no_refresh)
    _write_creds(p, "new-token", "new-refresh", 9999999999999)  # rotated by "someone else"
    assert client._token() == "new-token"
    client.close()


def test_token_reloads_disk_after_invalid_grant(tmp_path: Path, monkeypatch):
    """Our refresh attempt fails (single-use token already spent) but fresh
    tokens landed on disk meanwhile → recover from disk instead of raising."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        _write_creds(p, "new-token", "new-refresh", 9999999999999)
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    assert client._token() == "new-token"
    client.close()


def test_token_raises_when_no_recovery_possible(tmp_path: Path, monkeypatch):
    """Refresh fails and the disk still holds the same dead tokens → raise."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    with pytest.raises(CredentialsError):
        client._token()
    client.close()
