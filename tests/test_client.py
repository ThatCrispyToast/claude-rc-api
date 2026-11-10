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
    r = cli_control_request("set_model", "rid-1", model="claude-opus-5-5")
    assert r["type"] == "control_request"
    assert r["request"] == {"subtype": "set_model", "model": "claude-opus-5-5"}


def test_cli_control_request_effort_shape():
    # Effort rides apply_flag_settings (there is no set_effort subtype); an
    # explicit null effortLevel means "clear back to auto", so it must survive.
    r = cli_control_request(
        "apply_flag_settings", "rid-2", settings={"effortLevel": None, "ultracode": False}
    )
    assert r["request"] == {
        "subtype": "apply_flag_settings",
        "settings": {"effortLevel": None, "ultracode": False},
    }


# --- set_effort verdict handling (offline: transport methods stubbed) -------
def _bare_rc() -> "RemoteControlClient":
    from claude_rc.client import RemoteControlClient

    return RemoteControlClient.__new__(RemoteControlClient)  # skip __init__ / creds


def test_set_effort_control_accepted(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {"ok": 1})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, **kw: {"subtype": "success", "request_id": rid}
    )
    out = rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)
    assert out["via"] == "control"
    assert sent[0][0]["request"]["settings"] == {"effortLevel": "high", "ultracode": False}


def test_set_effort_command_fallback(monkeypatch):
    # Remote-control REPL workers refuse apply_flag_settings — the fallback
    # injects the /effort slash command (which they execute as a local command).
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc,
        "wait_control_response",
        lambda sid, rid, **kw: {
            "subtype": "error",
            "error": "REPL bridge does not handle control_request subtype: apply_flag_settings",
            "request_id": rid,
        },
    )
    monkeypatch.setattr(rc, "send_message", lambda sid, text, **kw: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)["via"] == "command"
    assert messages == ["/effort high"]
    assert rc.set_effort("cse_x", None, wait=1.0, command_fallback=True)["via"] == "command"
    assert messages[-1] == "/effort auto"


def test_set_effort_rejected_without_fallback(monkeypatch):
    from claude_rc.client import ControlRejected

    rc = _bare_rc()
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, **kw: {"subtype": "error", "error": "nope"}
    )
    try:
        rc.set_effort("cse_x", "low", wait=1.0)
        raise AssertionError("expected ControlRejected")
    except ControlRejected as exc:
        assert "nope" in str(exc)


def test_set_effort_timeout_no_fallback(monkeypatch):
    # No verdict within `wait` → assume applied, do NOT double-apply via command.
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: None)
    monkeypatch.setattr(rc, "send_message", lambda sid, text, **kw: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=0.1, command_fallback=True)["via"] == "control_unconfirmed"
    assert messages == []


def test_managed_agents_user_message_shape():
    assert user_message("x") == {"type": "user.message", "content": [{"type": "text", "text": "x"}]}


def test_managed_agents_stream_raises_apierror_on_non_200(monkeypatch):
    """A failed stream must surface as APIError.

    The body has to be read before it is formatted: httpx raises
    ``ResponseNotRead`` if ``.text`` is touched on a still-streaming response,
    which would mask the real status behind an unrelated exception.
    """
    from claude_rc.client import APIError, ManagedAgentsClient

    class _Resp:
        status_code = 503
        headers = {"request-id": "req_1"}
        read_called = False

        def read(self):
            type(self).read_called = True
            return b"upstream unavailable"

        @property
        def text(self):
            raise AssertionError("must not read .text of an unread stream")

        def iter_lines(self):
            raise AssertionError("must not parse the body of an error response")

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *exc):
            return False

    ma = ManagedAgentsClient(api_key="k")
    monkeypatch.setattr(ma._http, "stream", lambda *a, **kw: _Stream())
    with pytest.raises(APIError) as excinfo:
        list(ma.stream_events("sesn_1"))
    assert excinfo.value.status == 503
    assert excinfo.value.request_id == "req_1"
    assert "upstream unavailable" in excinfo.value.body
    assert _Resp.read_called
    ma.close()


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


# --- envelope + control-protocol accessors -----------------------------------
def _wire(payload, **envelope):
    return {"sequence_num": "7", "payload": payload, **envelope}


def test_event_envelope_accessors():
    e = Event.from_wire(_wire(
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        event_id="ev-1", source="client", created_at="2026-10-16T15:17:54Z",
        device_attestation_status="DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    ))
    assert (e.event_id, e.source, e.created_at) == ("ev-1", "client", "2026-10-16T15:17:54Z")
    assert e.attestation == "UNSPECIFIED"
    assert Event.from_wire({"type": "agent.message"}).attestation is None


def test_blocking_control_is_worker_prompts_only():
    def req(subtype, source):
        return Event.from_wire(_wire(
            {"type": "control_request", "request_id": "r1", "request": {"subtype": subtype}}, source=source
        ))

    assert req("can_use_tool", "worker").is_blocking_control
    assert req("request_user_dialog", "worker").is_blocking_control
    assert req("can_use_tool", None).is_blocking_control  # envelope without a source
    # A controller's side question (ours, or claude.ai's "btw") is not a prompt.
    assert not req("side_question", "client").is_blocking_control
    assert not req("side_question", "worker").is_blocking_control
    assert not req("can_use_tool", "client").is_blocking_control
    assert not req("interrupt", "client").is_blocking_control


def test_request_id_and_control_subtype():
    ask = Event.from_wire(_wire({"type": "control_request", "request_id": "r1", "request": {"subtype": "get_usage"}}))
    ans = Event.from_wire(_wire({"type": "control_response", "response": {"subtype": "success", "request_id": "r1"}}))
    assert (ask.request_id, ask.control_subtype) == ("r1", "get_usage")
    assert (ans.request_id, ans.control_subtype) == ("r1", None)
    assert Event.from_wire(_wire({"type": "assistant"})).request_id is None


def test_worker_state():
    init = Event.from_wire(_wire({
        "type": "system", "subtype": "init", "model": "claude-haiku-5-5", "effort": "max",
        "permissionMode": "bypassPermissions", "claude_code_version": "2.1.301", "cwd": "",
    }))
    assert init.worker_state == {
        "model": "claude-haiku-5-5", "effort": "max",
        "permission_mode": "bypassPermissions", "claude_code_version": "2.1.301",
    }
    status = Event.from_wire(_wire({"type": "system", "subtype": "status", "permissionMode": "plan", "status": None}))
    assert status.worker_state == {"permission_mode": "plan"}
    assert Event.from_wire(_wire({"type": "system", "subtype": "task_started"})).worker_state == {}


# --- answer builders ---------------------------------------------------------
from claude_rc.events import (  # noqa: E402
    cli_control_cancel_request,
    cli_dialog_response,
    cli_permission_response,
    cli_question_response,
)


def test_cli_user_message_pins_uuid():
    assert "uuid" not in cli_user_message("x")
    assert cli_user_message("x", message_uuid="u-1")["uuid"] == "u-1"


def test_cli_control_cancel_request_shape():
    assert cli_control_cancel_request("r9") == {"type": "control_cancel_request", "request_id": "r9"}


def test_cli_permission_response_shapes():
    allow = cli_permission_response("r1", allow=True, tool_input={"command": "ls"}, tool_use_id="toolu_1")
    assert allow == {"type": "control_response", "response": {
        "subtype": "success", "request_id": "r1", "tool_use_id": "toolu_1",
        "response": {"behavior": "allow", "updatedInput": {"command": "ls"}},
    }}
    # No input → no updatedInput at all (an empty one would blank the tool's input).
    assert cli_permission_response("r1", allow=True)["response"]["response"] == {"behavior": "allow"}
    deny = cli_permission_response("r2", allow=False)["response"]
    assert deny["response"] == {"behavior": "deny", "message": "Denied by controller"}
    assert "tool_use_id" not in deny


def test_cli_question_response_keeps_questions():
    questions = [{"question": "Pick one?", "header": "Pick", "options": [], "multiSelect": False}]
    r = cli_question_response("r3", {"Pick one?": "A"}, tool_input={"questions": questions}, tool_use_id="t")
    updated = r["response"]["response"]["updatedInput"]
    assert updated == {"questions": questions, "answers": {"Pick one?": "A"}}
    assert r["response"]["response"]["behavior"] == "allow"


def test_cli_dialog_response_shape():
    assert cli_dialog_response("r4", completed=False)["response"]["response"] == {"status": "cancelled", "result": {}}
    done = cli_dialog_response("r4", completed=True, result={"answers": ["x"]})
    assert done["response"]["response"] == {"status": "completed", "result": {"answers": ["x"]}}


# --- request/verdict plumbing (offline: transport stubbed) --------------------
def _ack(seq):
    return {"attestation_feedback": None, "results": [{"event_id": "e", "sequence_num": str(seq), "duplicate": False}]}


def _verdict_event(seq, request_id, **response):
    return Event.from_wire(_wire(
        {"type": "control_response", "response": {"request_id": request_id, **response}}, source="worker"
    ) | {"sequence_num": str(seq)})


def test_wait_control_response_scans_forward_from_the_request(monkeypatch):
    rc = _bare_rc()
    calls = []
    pages = {"40": [Event.from_wire(_wire({"type": "assistant"}) | {"sequence_num": "41"})],
             "41": [_verdict_event(42, "rid-1", subtype="success", response={"ok": 1})]}

    def fake_list(sid, **kw):
        calls.append(kw)
        return pages.get(kw.get("cursor"), [])

    monkeypatch.setattr(rc, "list_events", fake_list)
    monkeypatch.setattr("claude_rc.client.time.sleep", lambda s: None)
    verdict = rc.wait_control_response("cse_x", "rid-1", timeout=5, after_sequence_num=40)
    assert verdict == {"request_id": "rid-1", "subtype": "success", "response": {"ok": 1}}
    assert [c["cursor"] for c in calls] == ["40", "41"]  # the cursor advanced past seq 41
    assert all(c["sort_order"] == "asc" for c in calls)


def test_request_control_success_error_and_timeout(monkeypatch):
    from claude_rc.client import ControlRejected, ControlTimeout

    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or _ack(10))
    verdicts = iter([
        {"subtype": "success", "response": {"cancelled": True}},
        {"subtype": "error", "error": "stop_task: task_id must be a string"},
        None,
    ])
    seen_kw = []
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: seen_kw.append(kw) or next(verdicts))
    assert rc.cancel_queued_message("cse_x", "u-1") is True
    assert sent[0][0]["request"] == {"subtype": "cancel_async_message", "message_uuid": "u-1"}
    assert seen_kw[0]["after_sequence_num"] == 10
    with pytest.raises(ControlRejected, match="task_id"):
        rc.stop_task("cse_x", 123)
    with pytest.raises(ControlTimeout) as exc:
        rc.request_control("cse_x", "get_usage", timeout=0.1)
    assert exc.value.request_id.startswith("get_usage-")


def test_side_question_answer_and_cancel_on_timeout(monkeypatch):
    from claude_rc.client import ControlTimeout

    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or _ack(5))
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: {
        "subtype": "success", "request_id": rid, "response": {"response": "Writing tests.", "synthetic": False}})
    history = [{"question": "q0", "response": "a0", "extra": "dropped"}]
    assert rc.side_question("cse_x", "What now?", history=history) == "Writing tests."
    assert sent[0][0]["request"] == {
        "subtype": "side_question", "question": "What now?", "history": [{"question": "q0", "response": "a0"}],
    }

    sent.clear()
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: None)
    with pytest.raises(ControlTimeout):
        rc.side_question("cse_x", "Still there?", timeout=0.1)
    asked, cancelled = sent[0][0], sent[1][0]
    assert cancelled == {"type": "control_cancel_request", "request_id": asked["request_id"]}


def test_initialize_merges_pending_prompts(monkeypatch):
    rc = _bare_rc()
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: _ack(3))
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: {
        "subtype": "success", "response": {"claude_code_version": "2.1.301", "current_model": "m"},
        "pending_permission_requests": [{"request_id": "p"}], "pending_user_dialog_requests": [],
    })
    info = rc.initialize("cse_x")
    assert info["claude_code_version"] == "2.1.301"
    assert info["pending_permission_requests"] == [{"request_id": "p"}]


def test_session_reads_and_updates_are_unwrapped(monkeypatch):
    import httpx

    rc = _bare_rc()
    seen = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw.get("json")))
        body = {"response_shape": {"id": "cse_x", "title": "a"}} if method == "GET" else {"session": {"id": "cse_x", "title": "b"}}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(rc, "_request", fake_request)
    rc.base_url = "https://api.example"
    assert rc.get_session("cse_x") == {"id": "cse_x", "title": "a"}
    assert rc.rename_session("cse_x", "b") == {"id": "cse_x", "title": "b"}
    assert seen[1] == ("PUT", "https://api.example/v1/code/sessions/cse_x", {"title": "b"})


def test_send_message_message_uuid(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {})
    rc.send_message("cse_x", "hello", message_uuid="u-7")
    assert sent[0][0]["uuid"] == "u-7"
# --- macOS Keychain credential store -----------------------------------------
def _keychain_doc(token: str, refresh: str, expires_at_ms: int) -> str:
    return json.dumps({
        "mcpOAuth": {"srv": {"accessToken": "mcp"}},
        "claudeAiOauth": {
            "accessToken": token, "refreshToken": refresh,
            "expiresAt": expires_at_ms, "scopes": ["user:inference"],
            "subscriptionType": "max", "rateLimitTier": "tier",
            "refreshTokenExpiresAt": 123}})


@pytest.fixture
def fake_keychain(monkeypatch):
    """A darwin host whose Keychain is a dict and whose credentials file is absent."""
    from claude_rc import credentials as cr
    store: dict[str, str] = {}
    monkeypatch.setattr(cr.sys, "platform", "darwin")
    monkeypatch.delenv(cr.ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(cr.ENV_CLI_OAUTH_TOKEN, raising=False)
    monkeypatch.setattr(cr, "DEFAULT_CREDENTIALS_PATH", Path("/nonexistent/.credentials.json"))
    monkeypatch.setattr(cr, "_keychain_read", lambda svc: store.get(svc))
    monkeypatch.setattr(cr, "_keychain_write", lambda svc, secret: store.__setitem__(svc, secret))
    return store


def test_load_credentials_from_macos_keychain(fake_keychain):
    from claude_rc.credentials import KEYCHAIN_SERVICE
    fake_keychain[KEYCHAIN_SERVICE] = _keychain_doc("kc-token", "kc-refresh", 9999999999999)
    c = load_credentials()
    assert c.access_token == "kc-token"
    assert c.source_keychain == KEYCHAIN_SERVICE and c.source_path is None


def test_macos_missing_keychain_and_file_names_both(fake_keychain):
    with pytest.raises(CredentialsError, match="Keychain item"):
        load_credentials()


def test_explicit_path_skips_keychain(fake_keychain, tmp_path: Path):
    from claude_rc.credentials import KEYCHAIN_SERVICE
    fake_keychain[KEYCHAIN_SERVICE] = _keychain_doc("kc-token", "kc-refresh", 9999999999999)
    p = tmp_path / ".credentials.json"
    _write_creds(p, "file-token", "file-refresh", 9999999999999)
    assert load_credentials(p).access_token == "file-token"


def test_refresh_persists_back_to_keychain(fake_keychain):
    """A refresh spends the single-use refresh token, so the rotated pair must
    land back in the Keychain — or the CLI itself is logged out — without
    clobbering the document's other keys."""
    import httpx
    from claude_rc.credentials import KEYCHAIN_SERVICE, refresh_credentials
    fake_keychain[KEYCHAIN_SERVICE] = _keychain_doc("old-token", "old-refresh", 1)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
        "access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600}))
    with httpx.Client(transport=transport) as http:
        new = refresh_credentials(load_credentials(), client=http)
    assert new.access_token == "new-token"
    doc = json.loads(fake_keychain[KEYCHAIN_SERVICE])
    assert doc["mcpOAuth"] == {"srv": {"accessToken": "mcp"}}
    block = doc["claudeAiOauth"]
    assert (block["accessToken"], block["refreshToken"]) == ("new-token", "new-refresh")
    assert block["rateLimitTier"] == "tier"
    assert "refreshTokenExpiresAt" not in block


def test_client_reloads_rotated_keychain_tokens(fake_keychain, monkeypatch):
    from claude_rc.client import RemoteControlClient
    from claude_rc.credentials import KEYCHAIN_SERVICE
    fake_keychain[KEYCHAIN_SERVICE] = _keychain_doc("old-token", "old-refresh", 1)
    client = RemoteControlClient(org_uuid="org-x")

    def _no_refresh(*a, **kw):
        raise AssertionError("network refresh must not run when the Keychain has fresh tokens")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _no_refresh)
    fake_keychain[KEYCHAIN_SERVICE] = _keychain_doc("new-token", "new-refresh", 9999999999999)
    assert client._token() == "new-token"
    client.close()


def test_keychain_write_keeps_secret_off_argv(monkeypatch):
    from claude_rc import credentials as cr
    seen = {}

    def _run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    import subprocess
    monkeypatch.setattr(cr.subprocess, "run", _run)
    cr._keychain_write("svc", '{"s":1}')
    assert seen["argv"] == ["security", "-i"]
    assert '{"s":1}'.encode().hex() in seen["input"] and '{"s":1}' not in seen["input"]
    assert "-U" in seen["input"] and '-s "svc"' in seen["input"]


def test_keychain_write_long_document_goes_on_argv(monkeypatch):
    """`security -i` can't take a command past 4032 characters (a document with
    MCP logins gets there); the CLI then passes the same arguments on argv."""
    import subprocess
    from claude_rc import credentials as cr
    seen = {}

    def _run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cr.subprocess, "run", _run)
    monkeypatch.setenv("USER", "alice")
    secret = json.dumps({"mcpOAuth": {"srv": "x" * 3000}})
    cr._keychain_write("svc", secret)
    assert seen["argv"] == [
        "security", "add-generic-password", "-U", "-a", "alice", "-s", "svc", "-X", secret.encode().hex()]
    assert seen["kw"]["stdin"] is subprocess.DEVNULL and "input" not in seen["kw"]


def test_keychain_write_switches_to_argv_just_past_4032(monkeypatch):
    """The CLI pipes a command of up to 4032 characters into `security -i`."""
    import subprocess
    from claude_rc import credentials as cr
    seen = []
    monkeypatch.setattr(cr.subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    for user in ("alice", "alicex"):  # one of them makes 4032 reachable with whole bytes
        monkeypatch.setenv("USER", user)
        base = len(f'add-generic-password -U -a "{user}" -s "svc" -X ""\n')
        if (cr._KEYCHAIN_STDIN_MAX - base) % 2 == 0:
            break
    fits = "x" * ((cr._KEYCHAIN_STDIN_MAX - base) // 2)
    cr._keychain_write("svc", fits)
    cr._keychain_write("svc", fits + "x")
    assert seen[0] == ["security", "-i"] and seen[1][:2] == ["security", "add-generic-password"]


def test_keychain_account_matches_the_cli(monkeypatch):
    from claude_rc import credentials as cr
    monkeypatch.setenv("USER", "alice.b-c_1")
    assert cr._keychain_account() == "alice.b-c_1"
    monkeypatch.setenv("USER", "Alice Smith")  # the CLI won't file under this
    assert cr._keychain_account() == "claude-code-user"


def test_keychain_read_tells_missing_from_unreadable(monkeypatch):
    import subprocess
    from claude_rc import credentials as cr
    results = iter([(0, '{"a":1}\n'), (44, ""), (36, "")])

    def _run(argv, **kw):
        code, out = next(results)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(cr.subprocess, "run", _run)
    assert cr._keychain_read("svc") == '{"a":1}'
    assert cr._keychain_read("svc") is None  # 44: no such item
    with pytest.raises(CredentialsError, match="exited 36"):  # locked: not "empty"
        cr._keychain_read("svc")


def _locked_keychain(service):
    raise CredentialsError(f"could not read Keychain item {service!r}: security exited 36")


def test_unreadable_keychain_falls_back_to_the_file(fake_keychain, monkeypatch, tmp_path: Path):
    from claude_rc import credentials as cr
    monkeypatch.setattr(cr, "_keychain_read", _locked_keychain)
    with pytest.raises(CredentialsError, match="exited 36, and credentials file not found"):
        load_credentials()
    p = tmp_path / ".credentials.json"
    _write_creds(p, "file-token", "file-refresh", 9999999999999)
    monkeypatch.setattr(cr, "DEFAULT_CREDENTIALS_PATH", p)
    c = load_credentials()
    assert (c.access_token, c.source_path, c.source_keychain) == ("file-token", p, None)


def _token_endpoint(posts=None):
    """A token endpoint that rotates to new-token / new-refresh, logging each POST."""
    import httpx

    def answer(req):
        if posts is not None:
            posts.append(json.loads(req.content))
        return httpx.Response(200, json={
            "access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600})
    return httpx.Client(transport=httpx.MockTransport(answer))


def test_refresh_does_not_spend_the_token_when_the_keychain_is_unreadable(fake_keychain, monkeypatch):
    """A locked Keychain can't take the successor, so the single-use refresh
    token isn't spent at all (the CLI won't refresh then either)."""
    from claude_rc import credentials as cr
    fake_keychain[cr.KEYCHAIN_SERVICE] = before = _keychain_doc("old-token", "old-refresh", 1)
    creds = load_credentials()
    monkeypatch.setattr(cr, "_keychain_read", _locked_keychain)
    posts = []
    with _token_endpoint(posts) as http, pytest.raises(CredentialsError, match="exited 36"):
        cr.refresh_credentials(creds, client=http)
    assert posts == [] and fake_keychain[cr.KEYCHAIN_SERVICE] == before


def test_refresh_keeps_the_rest_of_the_credentials_file(tmp_path: Path):
    """Only the token fields change: the CLI's own block fields (including a
    subscriptionType it updated after we loaded) and the document's other keys
    stay; the spent token's expiry goes."""
    from claude_rc.credentials import refresh_credentials
    p = tmp_path / ".credentials.json"
    p.write_text(_keychain_doc("old-token", "old-refresh", 1))
    creds = load_credentials(p)
    doc = json.loads(p.read_text())
    doc["claudeAiOauth"]["subscriptionType"] = "pro"  # the CLI re-verified the plan meanwhile
    p.write_text(json.dumps(doc))
    with _token_endpoint() as http:
        refresh_credentials(creds, client=http)
    doc = json.loads(p.read_text())
    assert doc["mcpOAuth"] == {"srv": {"accessToken": "mcp"}}
    block = doc["claudeAiOauth"]
    assert (block["accessToken"], block["refreshToken"]) == ("new-token", "new-refresh")
    assert (block["subscriptionType"], block["rateLimitTier"]) == ("pro", "tier")
    assert "refreshTokenExpiresAt" not in block


def test_refresh_spends_nothing_once_the_store_has_moved_on(tmp_path: Path):
    """As the CLI's refresh: the shared refresh token is posted only while the
    store still holds this login. A sign-in (other tokens), a sign-out (gone)
    or a corrupt file raises first, with the token unspent and the store as is."""
    from claude_rc.credentials import refresh_credentials
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    creds = load_credentials(p)
    posts = []
    for moved_on in ('{"claudeAiOauth": {"accessToken": "signed-in", "refreshToken": "other"}}', "{not json", None):
        if moved_on is None:
            p.unlink()
        else:
            p.write_text(moved_on)
        with _token_endpoint(posts) as http, pytest.raises(CredentialsError, match="no longer holds"):
            refresh_credentials(creds, client=http)
        assert posts == [] and (p.read_text() if p.exists() else None) == moved_on


def test_refresh_that_loses_a_race_keeps_the_new_tokens(tmp_path: Path, caplog):
    """The store moves on while the POST is in flight (the CLI signed in again):
    the save is skipped, and the new pair still comes back, with a warning."""
    import httpx
    from claude_rc.credentials import refresh_credentials
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    creds = load_credentials(p)

    def answer(req):
        _write_creds(p, "signed-in", "signed-in-refresh", 9999999999999)
        return httpx.Response(200, json={"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        new = refresh_credentials(creds, client=http)
    assert new.access_token == "new-token" and new.stale_refresh_token == "old-refresh"
    assert json.loads(p.read_text())["claudeAiOauth"]["accessToken"] == "signed-in"
    assert "kept in memory only" in caplog.text


def test_refresh_returns_the_new_tokens_when_saving_fails(fake_keychain, monkeypatch, caplog):
    """Once the refresh token is spent the new pair is the only valid one, so a
    failed save logs instead of dropping it, even where warnings are errors."""
    import warnings
    from claude_rc import credentials as cr
    fake_keychain[cr.KEYCHAIN_SERVICE] = _keychain_doc("old-token", "old-refresh", 1)
    creds = load_credentials()

    def _write_fails(service, secret):
        raise CredentialsError(f"could not update Keychain item {service!r}: timed out")

    monkeypatch.setattr(cr, "_keychain_write", _write_fails)
    with _token_endpoint() as http, warnings.catch_warnings():
        warnings.simplefilter("error")
        new = cr.refresh_credentials(creds, client=http)
    assert (new.access_token, new.refresh_token) == ("new-token", "new-refresh")
    assert new.stale_refresh_token == "old-refresh"
    assert "kept in memory only" in caplog.text and "timed out" in caplog.text


def test_refresh_survives_a_failed_file_write(tmp_path: Path, monkeypatch, caplog):
    """The live Linux path: the replace fails, the new pair comes back, and the
    temporary file holding it is gone."""
    from claude_rc import credentials as cr
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    before = p.read_text()
    creds = load_credentials(p)

    def _replace_fails(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cr.os, "replace", _replace_fails)
    with _token_endpoint() as http:
        new = cr.refresh_credentials(creds, client=http)
    assert (new.access_token, new.stale_refresh_token) == ("new-token", "old-refresh")
    assert p.read_text() == before and list(tmp_path.iterdir()) == [p]
    assert "could not write" in caplog.text


def test_client_keeps_unsaved_tokens_and_saves_them_later(tmp_path: Path, monkeypatch):
    """After a refresh that couldn't be saved, the store still holds the spent
    pair (or the "" the CLI clears it to). The client must not reload that over
    the live pair, and its next refresh posts the live refresh token and saves."""
    from claude_rc import credentials as cr
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)
    posts = []
    client._refresher.close()
    client._refresher = _token_endpoint(posts)
    real_replace = cr.os.replace

    def _replace_fails(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cr.os, "replace", _replace_fails)
    assert client._token() == "new-token"  # refreshed, not saved
    monkeypatch.setattr(cr.os, "replace", real_replace)
    for cleared in (False, True):
        if cleared:  # the CLI's own refresh hit the spent token and cleared it
            doc = json.loads(p.read_text())
            doc["claudeAiOauth"]["refreshToken"] = ""
            p.write_text(json.dumps(doc))
        assert not client._reload_credentials()
        assert client._creds.refresh_token == "new-refresh"
    client._creds.expires_at_ms = 1  # the live pair expires in turn
    assert client._token() == "new-token"
    assert [b["refresh_token"] for b in posts] == ["old-refresh", "new-refresh"]
    assert json.loads(p.read_text())["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert client._creds.stale_refresh_token is None
    client.close()


def test_refresh_saves_over_a_token_the_cli_cleared(tmp_path: Path):
    """The CLI clears a refresh token it found dead to ""; its compare-and-swap
    still saves over that, and so does ours."""
    import httpx
    from claude_rc.credentials import refresh_credentials
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    creds = load_credentials(p)

    def answer(req):
        doc = json.loads(p.read_text())
        doc["claudeAiOauth"]["refreshToken"] = ""
        p.write_text(json.dumps(doc))
        return httpx.Response(200, json={"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        assert refresh_credentials(creds, client=http).stale_refresh_token is None
    assert json.loads(p.read_text())["claudeAiOauth"]["refreshToken"] == "new-refresh"
    # Cleared before the POST, it is a dead login: nothing is spent into it.
    doc = json.loads(p.read_text())
    doc["claudeAiOauth"]["refreshToken"] = ""
    p.write_text(json.dumps(doc))
    posts = []
    with _token_endpoint(posts) as http, pytest.raises(CredentialsError, match="no longer holds"):
        refresh_credentials(creds, client=http)
    assert posts == []


def test_unparsable_keychain_item_falls_back_to_the_file(fake_keychain, monkeypatch, tmp_path: Path):
    from claude_rc import credentials as cr
    fake_keychain[cr.KEYCHAIN_SERVICE] = "{not json"
    with pytest.raises(CredentialsError, match="not valid JSON, and credentials file not found"):
        load_credentials()
    p = tmp_path / ".credentials.json"
    _write_creds(p, "file-token", "file-refresh", 9999999999999)
    monkeypatch.setattr(cr, "DEFAULT_CREDENTIALS_PATH", p)
    assert load_credentials().access_token == "file-token"


def test_explicit_credentials_reload_from_their_own_file(tmp_path: Path, monkeypatch):
    """Rotated tokens are re-read from the store the credentials came from,
    not from the default one (which may hold another account)."""
    from claude_rc import credentials as cr
    from claude_rc.client import RemoteControlClient
    default, other = tmp_path / "default.json", tmp_path / "other.json"
    _write_creds(default, "default-token", "default-refresh", 9999999999999)
    _write_creds(other, "other-old", "other-refresh", 1)
    monkeypatch.setattr(cr, "DEFAULT_CREDENTIALS_PATH", default)
    monkeypatch.delenv(cr.ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(cr.ENV_CLI_OAUTH_TOKEN, raising=False)
    client = RemoteControlClient(credentials=load_credentials(other), org_uuid="org-x")
    posts = []
    client._refresher.close()
    client._refresher = _token_endpoint(posts)
    _write_creds(other, "other-new", "other-refresh-2", 9999999999999)
    assert client._token() == "other-new" and posts == []
    client.close()
    # Explicit credentials from the default file keep the CLI's own lookup,
    # which on macOS reads the Keychain first.
    client = RemoteControlClient(credentials=load_credentials(), org_uuid="org-x")
    assert client._creds_path is None
    client.close()


def test_client_does_not_revive_a_signed_out_login(tmp_path: Path):
    """After a sign-out deletes the credentials file, an expired client fails
    instead of refreshing the old login back into existence."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)
    posts = []
    client._refresher.close()
    client._refresher = _token_endpoint(posts)
    p.unlink()
    with pytest.raises(CredentialsError, match="no longer holds"):
        client._token()
    assert posts == [] and not p.exists()
    client.close()
# --- trusted-device lazy enrollment (offline: transport stubbed) -------------
@pytest.fixture(autouse=True)
def _fresh_enrollment_floor(monkeypatch):
    """The 5-minute enrollment floor is per process; give each test its own."""
    monkeypatch.setattr("claude_rc.client._last_enrollment", float("-inf"))
    monkeypatch.delenv("CLAUDE_TRUSTED_DEVICE_TOKEN", raising=False)


class _FakeResp:
    def __init__(self, status, text="", json_body=None):
        self.status_code = status
        self.text = text
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json


class _FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self.responses.pop(0)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("headers", {})))
        return self.responses.pop(0)


def _oauth_creds(**kw):
    base = dict(access_token="tok", refresh_token="ref", expires_at_ms=9999999999999,
                scopes=["user:inference"], subscription_type="max")
    base.update(kw)
    return OAuthCredentials(**base)


def test_enroll_trusted_device_success(tmp_path: Path):
    from claude_rc.credentials import enroll_trusted_device

    p = tmp_path / ".credentials.json"
    _write_creds(p, "tok", "ref", 9999999999999)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceEnrolledAfterRefusalAtMs": 1}))
    creds = _oauth_creds(source_path=p)
    fake = _FakeHttp([_FakeResp(200, json_body={"device_token": "dev-tok-1"})])

    token = enroll_trusted_device(creds, client=fake)

    assert token == "dev-tok-1"
    assert creds.trusted_device_token == "dev-tok-1"
    saved = json.loads(p.read_text())
    assert saved["trustedDeviceToken"] == "dev-tok-1"  # top-level, like the CLI
    assert saved["claudeAiOauth"]["accessToken"] == "tok"  # other keys preserved
    assert "trustedDeviceEnrolledAfterRefusalAtMs" not in saved  # not a refusal's enrollment
    assert fake.calls[0][1].endswith("/api/auth/trusted_devices")


def test_enroll_trusted_device_declined(tmp_path: Path):
    from claude_rc.credentials import enroll_trusted_device

    p = tmp_path / ".credentials.json"
    _write_creds(p, "tok", "ref", 9999999999999)
    creds = _oauth_creds(source_path=p)
    fake = _FakeHttp([_FakeResp(403, "forbidden")])

    assert enroll_trusted_device(creds, client=fake) is None
    assert creds.trusted_device_token is None
    assert "trustedDeviceToken" not in json.loads(p.read_text())


_REFUSED = '{"error":{"resource":"untrusted_device"}}'
_EVENTS_URL = "https://api.anthropic.com/v1/code/sessions/x/events"


def _bare_rc_with(creds, http):
    from claude_rc.client import RemoteControlClient

    rc = RemoteControlClient.__new__(RemoteControlClient)
    rc._creds = creds
    rc._creds_path = creds.source_path
    rc._persist_refresh = True
    rc._auto_refresh = False
    rc.base_url = "https://api.anthropic.com"
    rc._org_uuid = None
    rc._http = http
    rc._refresher = http
    return rc


def _stored_creds(tmp_path: Path):
    """A credentials file and the credentials loaded from it."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "tok", "ref", 9999999999999)
    return p, load_credentials(p)


def _enrolls_dev_tok_1(c, **kw):
    c.trusted_device_token = "dev-tok-1"  # the real one sets it on the creds too
    return "dev-tok-1"


def test_device_token_refusal_kinds():
    """Only a refused token is worth enrolling for, as the CLI reads the 403."""
    from claude_rc.client import _device_token_refused

    def body(**error):
        return json.dumps({"error": {"resource": "untrusted_device", **error}})

    assert _device_token_refused(body())
    assert _device_token_refused(body(sub_reason=""))
    assert _device_token_refused(body(sub_reason="bound_unattested"))
    assert not _device_token_refused(body(sub_reason="served_call_unsigned"))  # wants a signature
    assert not _device_token_refused(body(sub_reason="something_new"))
    assert not _device_token_refused('{"error":{"resource":"session_stale_relogin"}}')
    assert not _device_token_refused("upstream says untrusted_device")
    assert not _device_token_refused("[1]")


def test_request_enrolls_and_retries_on_untrusted_device(tmp_path: Path, monkeypatch):
    _, creds = _stored_creds(tmp_path)
    http = _FakeHttp([
        _FakeResp(403, _REFUSED),
        _FakeResp(200, "{}", {"ok": True}),
    ])
    rc = _bare_rc_with(creds, http)
    calls = []
    monkeypatch.setattr(
        "claude_rc.client.enroll_trusted_device", lambda c, **kw: calls.append(kw) or _enrolls_dev_tok_1(c))

    resp = rc._request("GET", _EVENTS_URL, extra_headers={"anthropic-beta": "b"})

    assert resp.status_code == 200
    assert len(calls) == 1 and calls[0]["after_refusal"] is True  # enrolled exactly once
    retry_headers = http.calls[1][2]
    assert retry_headers.get("X-Trusted-Device-Token") == "dev-tok-1"
    assert retry_headers.get("anthropic-beta") == "b"  # the retry keeps the caller's headers


def test_lazy_enrollment_is_stamped_and_not_repeated(tmp_path: Path, monkeypatch):
    """Through the real enroll: the token and its refusal stamp land in the
    store, and when the server refuses the new token too, nothing enrolls
    again, in this client or a restarted one (each enrollment emails the
    account owner)."""
    p, creds = _stored_creds(tmp_path)
    http = _FakeHttp([
        _FakeResp(403, _REFUSED),
        _FakeResp(200, json_body={"device_token": "dev-tok-1"}),  # the enrollment POST
        _FakeResp(403, _REFUSED),  # the retry, refused as well
        _FakeResp(403, _REFUSED),  # a later call
    ])
    rc = _bare_rc_with(creds, http)
    assert rc._request("GET", _EVENTS_URL).status_code == 403
    doc = json.loads(p.read_text())
    assert doc["trustedDeviceToken"] == "dev-tok-1" and doc["trustedDeviceEnrolledAfterRefusalAtMs"] > 0
    assert doc["claudeAiOauth"]["accessToken"] == "tok"
    monkeypatch.setattr("claude_rc.client._last_enrollment", float("-inf"))  # only the stamp guards now
    assert rc._request("GET", _EVENTS_URL).status_code == 403
    assert [c[0] for c in http.calls] == ["GET", "POST", "GET", "GET"]
    restarted = _bare_rc_with(load_credentials(p), _FakeHttp([_FakeResp(403, _REFUSED)]))
    assert restarted._request("GET", _EVENTS_URL).status_code == 403
    assert [c[0] for c in restarted._http.calls] == ["GET"]


def test_request_takes_a_token_the_store_gained(tmp_path: Path, monkeypatch):
    """The CLI enrolled after this client loaded: retry with its token."""
    p, creds = _stored_creds(tmp_path)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "cli-token"}))
    http = _FakeHttp([_FakeResp(403, _REFUSED), _FakeResp(200, "{}", {"ok": True})])
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    assert _bare_rc_with(creds, http)._request("GET", _EVENTS_URL).status_code == 200
    assert http.calls[1][2].get("X-Trusted-Device-Token") == "cli-token"


def test_no_lazy_enrollment_without_a_store_to_guard_it(tmp_path: Path, monkeypatch):
    """The guard against re-enrolling lives in the store, so env-token
    credentials and a client told not to write surface the 403 instead."""
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    no_store = _bare_rc_with(_oauth_creds(), _FakeHttp([_FakeResp(403, _REFUSED)]))
    assert no_store._request("GET", _EVENTS_URL).status_code == 403
    _, creds = _stored_creds(tmp_path)
    in_memory = _bare_rc_with(creds, _FakeHttp([_FakeResp(403, _REFUSED)]))
    in_memory._persist_refresh = False
    assert in_memory._request("GET", _EVENTS_URL).status_code == 403


def test_request_does_not_loop_when_enrollment_fails(tmp_path: Path, monkeypatch):
    """A declined enrollment surfaces the 403, and another refusal within
    5 minutes doesn't try again."""
    _, creds = _stored_creds(tmp_path)
    tries = []
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda c, **kw: tries.append(kw))
    rc = _bare_rc_with(creds, _FakeHttp([_FakeResp(403, _REFUSED), _FakeResp(403, _REFUSED)]))
    assert rc._request("GET", _EVENTS_URL).status_code == 403
    assert rc._request("GET", _EVENTS_URL).status_code == 403  # original error; no retry storm
    assert len(tries) == 1


def test_stream_reconnects_after_a_recovered_refusal(tmp_path: Path, monkeypatch):
    import httpx

    _, creds = _stored_creds(tmp_path)
    sent = []

    def handler(req):
        sent.append(req.headers.get("X-Trusted-Device-Token"))
        if len(sent) == 1:
            return httpx.Response(403, text=_REFUSED)
        frame = 'event: client_event\nid: 1\ndata: {"sequence_num": 1, "payload": {"type": "result"}}\n\n'
        return httpx.Response(200, text=frame)

    rc = _bare_rc_with(creds, httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", _enrolls_dev_tok_1)
    assert [e.type for e in rc.stream_events("cse_x", reconnect=False)] == ["result"]
    assert sent == [None, "dev-tok-1"]
    rc._http.close()


def test_enrollment_saves_only_for_its_own_login(tmp_path: Path, caplog):
    """A store signed out, or signed in as someone else, since is left alone;
    the token still serves from memory, and saving never raises (the device
    exists and its owner has been emailed by then)."""
    from claude_rc.credentials import enroll_trusted_device

    p, creds = _stored_creds(tmp_path)
    for since in ("{}", '{"claudeAiOauth": {"accessToken": "someone-else"}}'):
        p.write_text(since)
        fake = _FakeHttp([_FakeResp(200, json_body={"device_token": "dev-tok-1"})])
        assert enroll_trusted_device(creds, client=fake) == "dev-tok-1" == creds.trusted_device_token
        assert p.read_text() == since
    assert "kept in memory only" in caplog.text


def test_enrollment_save_failure_returns_the_token(tmp_path: Path, monkeypatch, caplog):
    from claude_rc import credentials as cr

    p, creds = _stored_creds(tmp_path)

    def _replace_fails(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cr.os, "replace", _replace_fails)
    fake = _FakeHttp([_FakeResp(200, json_body={"device_token": "dev-tok-1"})])
    assert cr.enroll_trusted_device(creds, client=fake, after_refusal=True) == "dev-tok-1"
    assert "trustedDeviceToken" not in json.loads(p.read_text()) and "could not write" in caplog.text


def test_enroll_returns_none_when_the_request_fails():
    import httpx
    from claude_rc.credentials import enroll_trusted_device

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    with httpx.Client(transport=httpx.MockTransport(boom)) as http:
        assert enroll_trusted_device(_oauth_creds(), client=http) is None


def test_recovery_follows_a_sign_in_before_enrolling(tmp_path: Path, monkeypatch):
    """The CLI signed in as another login (and enrolled it) after this client
    loaded: retry as that login, with its token, rather than enroll the old one."""
    p, creds = _stored_creds(tmp_path)
    _write_creds(p, "b-token", "b-refresh", 9999999999999)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "b-dev"}))
    http = _FakeHttp([_FakeResp(403, _REFUSED), _FakeResp(200, "{}", {"ok": True})])
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    assert _bare_rc_with(creds, http)._request("GET", _EVENTS_URL).status_code == 200
    retry = http.calls[1][2]
    assert (retry["Authorization"], retry["X-Trusted-Device-Token"]) == ("Bearer b-token", "b-dev")


def test_no_enrollment_after_a_sign_out(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    p, creds = _stored_creds(tmp_path)
    for gone in ("{}", None):
        if gone is None:
            p.unlink()
        else:
            p.write_text(gone)
        rc = _bare_rc_with(creds, _FakeHttp([_FakeResp(403, _REFUSED)]))
        assert rc._request("GET", _EVENTS_URL).status_code == 403


def test_enrollment_floor_is_per_process(tmp_path: Path):
    """Declined enrollments write no stamp, so only the 5-minute floor holds
    them back, across every client in the process."""
    _, creds = _stored_creds(tmp_path)
    posts = 0
    for _ in range(3):
        http = _FakeHttp([_FakeResp(403, _REFUSED), _FakeResp(403, '{"error": "declined"}')])
        _bare_rc_with(creds, http)._request("GET", _EVENTS_URL)
        posts += sum(c[0] == "POST" for c in http.calls)
    assert posts == 1


def test_env_trusted_device_token_is_sent_and_stops_enrollment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_TRUSTED_DEVICE_TOKEN", "provisioned")
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    p, _ = _stored_creds(tmp_path)
    http = _FakeHttp([_FakeResp(403, _REFUSED)])
    assert _bare_rc_with(load_credentials(p), http)._request("GET", _EVENTS_URL).status_code == 403
    assert http.calls[0][2]["X-Trusted-Device-Token"] == "provisioned"


@pytest.mark.parametrize("stamp", [10**15, 0, -1, 1.5, True, None])
def test_unusable_refusal_stamp_blocks_enrollment(tmp_path: Path, monkeypatch, stamp):
    """As the CLI: a refused token stamped in the future, or with a stamp that
    isn't a positive whole number (null included), doesn't enroll again."""
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    p, _ = _stored_creds(tmp_path)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "dev",
                             "trustedDeviceEnrolledAfterRefusalAtMs": stamp}))
    rc = _bare_rc_with(load_credentials(p), _FakeHttp([_FakeResp(403, _REFUSED)]))
    assert rc._request("GET", _EVENTS_URL).status_code == 403


def test_stored_token_with_a_recent_stamp_is_still_adopted(tmp_path: Path, monkeypatch):
    """The stamp only stops re-enrolling the token it describes; a different
    stored token (another process enrolled it after a refusal) is used."""
    import time

    p, creds = _stored_creds(tmp_path)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "theirs",
                             "trustedDeviceEnrolledAfterRefusalAtMs": int(time.time() * 1000)}))
    http = _FakeHttp([_FakeResp(403, _REFUSED), _FakeResp(200, "{}", {"ok": True})])
    monkeypatch.setattr("claude_rc.client.enroll_trusted_device", lambda *a, **kw: pytest.fail("enrolled"))
    assert _bare_rc_with(creds, http)._request("GET", _EVENTS_URL).status_code == 200
    assert http.calls[1][2]["X-Trusted-Device-Token"] == "theirs"


def test_stream_raises_when_a_refusal_cant_be_recovered(tmp_path: Path):
    import httpx

    from claude_rc.client import APIError

    _, creds = _stored_creds(tmp_path)
    connects = []

    def handler(req):
        if req.method == "POST":
            return httpx.Response(403, json={"error": "declined"})
        connects.append(req.url.path)
        return httpx.Response(403, text=_REFUSED)

    rc = _bare_rc_with(creds, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(APIError) as exc:
        list(rc.stream_events("cse_x", reconnect=True))
    assert exc.value.status == 403 and len(connects) == 1
    rc._http.close()


def test_401_retry_keeps_extra_headers(tmp_path: Path):
    p, creds = _stored_creds(tmp_path)
    _write_creds(p, "rotated", "rotated-refresh", 9999999999999)  # another process refreshed
    http = _FakeHttp([_FakeResp(401, "{}"), _FakeResp(200, "{}", {"ok": True})])
    rc = _bare_rc_with(creds, http)
    rc._auto_refresh = True
    assert rc._request("GET", _EVENTS_URL, extra_headers={"anthropic-beta": "b"}).status_code == 200
    assert http.calls[1][2]["Authorization"] == "Bearer rotated" and http.calls[1][2]["anthropic-beta"] == "b"


def test_refresh_keeps_a_device_token_enrolled_since(tmp_path: Path):
    """A refresh saves only the OAuth block, so a trusted-device token the CLI
    enrolled after this process loaded survives it."""
    from claude_rc.credentials import refresh_credentials

    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "old-dev"}))
    creds = load_credentials(p)
    p.write_text(json.dumps({**json.loads(p.read_text()), "trustedDeviceToken": "new-dev"}))
    with _token_endpoint() as http:
        refresh_credentials(creds, client=http)
    assert json.loads(p.read_text())["trustedDeviceToken"] == "new-dev"


# --- refreshTokenExpiresAt: when the login ends ---------------------------------
@pytest.mark.parametrize("stored, granted_s, kept", [
    ("soon", None, "soon"),      # the response names no lifetime: the stored end stays
    ("soon", 86400 * 30, "soon"),  # a refresh never moves the end later
    ("later", 3600, "granted"),  # …but an earlier one in the response wins
    (123, 3600, None),           # not a time the CLI would trust: removed
    (True, None, None),
    (None, 3600, None),          # the login recorded no end: none is invented
])
def test_refresh_keeps_when_the_login_ends(tmp_path: Path, stored, granted_s, kept):
    import httpx

    from claude_rc import credentials as cr

    now = cr._now_ms()
    ends = {"soon": now + 86400_000, "later": now + 90 * 86400_000}
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    doc = json.loads(p.read_text())
    if stored is not None:
        doc["claudeAiOauth"]["refreshTokenExpiresAt"] = ends.get(stored, stored)
        p.write_text(json.dumps(doc))
    body = {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600}
    if granted_s is not None:
        body["refresh_token_expires_in"] = granted_s
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as http:
        cr.refresh_credentials(load_credentials(p), client=http)
    saved = json.loads(p.read_text())["claudeAiOauth"].get("refreshTokenExpiresAt")
    if kept == "granted":
        assert now + 3600_000 <= saved < now + 3700_000
    else:
        assert saved == ends.get(kept)
