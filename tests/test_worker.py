"""Offline tests for the worker side: a stub seat and a scripted `/worker` API."""
import itertools
import json

import httpx
import pytest

from claude_rc import RemoteControlClient, RemoteControlWorker, WorkerEvicted
from claude_rc.client import APIError
from claude_rc.events import (
    Event,
    worker_assistant,
    worker_compact_boundary,
    worker_control_response,
    worker_conversation_reset,
    worker_init,
    worker_initialize_response,
    worker_permission_request,
    worker_result,
    worker_shutting_down,
    worker_status,
    worker_stream_event,
    worker_tool_result,
)

SID = "cse_x"
BASE = f"https://api.example/v1/code/sessions/{SID}"


class _Seat:
    """Stands in for RemoteControlClient: each claim starts the next epoch."""

    def __init__(self):
        self.claims = []
        self.archived = []

    def archive_session(self, session_id):
        self.archived.append(session_id)
        return True

    def bridge_session(self, session_id, *, expected_epoch=None):
        self.claims.append(expected_epoch)
        n = len(self.claims)
        return {"worker_jwt": f"jwt-{n}", "worker_epoch": str(n), "api_base_url": "https://api.example/", "expires_in": 3600}


def _sse(*envelopes):
    frames = [f"event: client_event\nid: {e['sequence_num']}\ndata: {json.dumps(e)}\n\n" for e in envelopes]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=":keepalive\n\n" + "".join(frames))


def _envelope(seq, payload):
    return {"sequence_num": str(seq), "event_id": f"ev-{seq}", "source": "client", "payload": payload}


@pytest.fixture
def api(monkeypatch):
    """A connected worker over a scripted API: `calls` records every request as
    (method, path, token, body), `answers` maps (method, path) to a response,
    a list of them to go through, or a function of the request."""
    monkeypatch.setattr("claude_rc.worker._HEARTBEAT_S", 3600.0)
    calls, answers = [], {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/v1/code/sessions/{SID}")
        token = request.headers["authorization"].removeprefix("Bearer ")
        calls.append((request.method, path, token, json.loads(request.content) if request.content else None))
        answer = answers.get((request.method, path), httpx.Response(200, json={}))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return answer(request) if callable(answer) else answer

    seat = _Seat()
    worker = RemoteControlWorker(seat, SID)
    worker._http = httpx.Client(transport=httpx.MockTransport(handle))
    answers[("POST", "/worker/heartbeat")] = httpx.Response(200, json={"heartbeat_interval_seconds": 3600})
    worker.connect()
    yield worker, seat, calls, answers
    worker.close()


def _sent(calls, method, path):
    return [body for m, p, _, body in calls if (m, p) == (method, path)]


# --- the seat ----------------------------------------------------------------
def test_connect_claims_the_seat_and_registers_idle(api):
    worker, seat, calls, _ = api
    assert seat.claims == [None]  # an unconditional claim: it evicts whoever held the seat
    assert worker.epoch == 1
    method, path, token, body = calls[0]
    assert (method, path, token) == ("PUT", "/worker", "jwt-1")
    assert body["worker_epoch"] == 1 and body["worker_status"] == "idle"


def test_close_says_goodbye_as_the_current_epoch(api):
    worker, _, calls, _ = api
    worker.close()
    assert _sent(calls, "PUT", "/worker")[-1] == {"worker_epoch": 1, "connection_status": "disconnected"}
    worker.close()  # a second close is a no-op


def test_close_can_end_the_session_once_it_has_said_goodbye(api):
    """As Claude Code ends its own on exit. Archived first, the session would
    take no goodbye and stay listed as connected."""
    worker, seat, calls, answers = api
    answers[("PUT", "/worker")] = lambda request: (
        httpx.Response(200, json={}) if not seat.archived else httpx.Response(409, json={}))
    worker.close(archive=True)
    assert _sent(calls, "PUT", "/worker")[-1] == {"worker_epoch": 1, "connection_status": "disconnected"}
    assert seat.archived == [SID]


def test_a_worker_that_lost_its_seat_leaves_the_session_to_its_successor(api):
    worker, seat, _, answers = api
    answers[("POST", "/worker/events")] = httpx.Response(409, json={"error": {
        "type": "epoch_conflict", "reason": "superseded_by_worker", "message": "epoch mismatch"}})
    with pytest.raises(WorkerEvicted):
        worker.send({"type": "system"})
    worker.close(archive=True)
    assert seat.archived == []


def test_a_conflict_evicts_the_worker_for_good(api):
    worker, _, calls, answers = api
    answers[("POST", "/worker/events")] = httpx.Response(409, json={"error": {
        "type": "epoch_conflict", "reason": "superseded_by_worker", "message": "epoch mismatch"}})
    with pytest.raises(WorkerEvicted) as refused:
        worker.send({"type": "assistant"})
    assert refused.value.reason == "superseded_by_worker"
    before = len(calls)
    with pytest.raises(WorkerEvicted):
        worker.report_state("idle")
    assert len(calls) == before  # nothing more is sent as an evicted worker
    worker.close()
    assert _sent(calls, "PUT", "/worker")[-1].get("connection_status") is None  # nor a goodbye


def test_a_401_renews_the_token_without_taking_the_seat_from_a_successor(api):
    worker, seat, calls, answers = api
    worker.report_state("running")
    answers[("POST", "/worker/events")] = [httpx.Response(401), httpx.Response(200, json={"results": []})]
    worker.send({"type": "assistant"})
    assert seat.claims == [None, 1]  # the renewal is conditional on still holding epoch 1
    posts = [(token, body["worker_epoch"]) for m, p, token, body in calls if (m, p) == ("POST", "/worker/events")]
    assert posts == [("jwt-1", 1), ("jwt-2", 2)]
    # The new epoch is told the worker's state before anything else is sent under it.
    registered = [c for c in calls if c[2] == "jwt-2"][0]
    assert registered[:2] == ("PUT", "/worker") and registered[3] == {"worker_epoch": 2, "worker_status": "running"}


def test_a_token_near_its_end_is_renewed_before_the_call(api):
    worker, seat, calls, _ = api
    worker._renew_at = 0.0
    worker.report_delivery("ev-1", "processed")
    assert seat.claims == [None, 1]
    assert calls[-1][2] == "jwt-2" and calls[-1][3]["worker_epoch"] == 2


def test_transient_failures_are_retried(api, monkeypatch):
    worker, _, calls, answers = api
    monkeypatch.setattr(worker._closed, "wait", lambda timeout=None: False)  # no real backoff
    answers[("POST", "/worker/events")] = [
        httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"results": [], "has_subscribers": True})]
    assert worker.send({"type": "assistant"})["has_subscribers"] is True
    assert len(_sent(calls, "POST", "/worker/events")) == 3


def test_a_refusal_that_is_not_transient_raises(api):
    worker, _, calls, answers = api
    answers[("POST", "/worker/events")] = httpx.Response(413, text="too large")
    with pytest.raises(APIError) as refused:
        worker.send({"type": "assistant"})
    assert refused.value.status == 413
    assert len(_sent(calls, "POST", "/worker/events")) == 1


# --- inbound -------------------------------------------------------------------
def test_events_yields_what_controllers_send_and_acknowledges_it(api):
    worker, _, calls, answers = api
    user = {"type": "user", "uuid": "u-1", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    control = {"type": "control_request", "request_id": "r-1", "request": {"subtype": "initialize"}}
    answers[("GET", "/worker/events/stream")] = _sse(_envelope(4, user), _envelope(5, control), _envelope(5, control))
    first, second = itertools.islice(worker.events(), 2)
    assert (first.type, first.text(), first.sequence_num) == ("user", "hi", 4)
    assert second.control_subtype == "initialize"
    receipts = _sent(calls, "POST", "/worker/events/delivery")
    assert [r["updates"] for r in receipts] == [
        [{"event_id": "ev-4", "status": "received"}], [{"event_id": "ev-5", "status": "received"}]]


def test_events_redials_from_where_it_got_to(api, monkeypatch):
    worker, _, calls, answers = api
    monkeypatch.setattr(worker._closed, "wait", lambda timeout=None: False)
    message = {"type": "user", "message": {"role": "user", "content": "x"}}
    # The first connection ends after one event; the redial replays it and adds one.
    answers[("GET", "/worker/events/stream")] = [
        _sse(_envelope(7, message)), _sse(_envelope(7, message), _envelope(8, message))]
    assert [e.sequence_num for e in itertools.islice(worker.events(), 2)] == [7, 8]
    dials = [c for c in calls if c[:2] == ("GET", "/worker/events/stream")]
    assert len(dials) == 2


def test_events_raises_when_the_seat_is_gone(api):
    worker, _, _, answers = api
    answers[("GET", "/worker/events/stream")] = httpx.Response(409, json={"error": {"reason": "session_not_active"}})
    with pytest.raises(WorkerEvicted) as refused:
        next(worker.events())
    assert refused.value.reason == "session_not_active"


# --- outbound ------------------------------------------------------------------
def test_send_wraps_events_and_marks_ephemeral_ones(api):
    worker, _, calls, _ = api
    worker.send({"type": "assistant", "uuid": "keep-me"}, {"type": "result"})
    worker.send({"type": "stream_event"}, ephemeral=True)
    stored, live = _sent(calls, "POST", "/worker/events")
    assert stored["worker_epoch"] == 1
    assert stored["events"][0] == {"payload": {"uuid": "keep-me", "session_id": SID, "type": "assistant"}}
    assert stored["events"][1]["payload"]["uuid"]  # minted
    assert live["events"][0]["ephemeral"] is True


def test_a_turn_reports_its_messages_and_its_state(api):
    worker, _, calls, _ = api
    message = Event.from_wire(_envelope(3, {"type": "user", "uuid": "u-3"}))
    del calls[:]
    worker.begin_turn(message)
    worker.end_turn(message, result="done")
    steps = [(m, p, body.get("worker_status") or (body.get("updates") or body.get("events"))[0]) for m, p, _, body in calls]
    assert steps[0] == ("POST", "/worker/events/delivery", {"event_id": "ev-3", "status": "processing"})
    assert steps[1] == ("PUT", "/worker", "running")
    result = steps[2][2]["payload"]
    assert (result["type"], result["subtype"], result["result"], result["user_message_uuid"]) == ("result", "success", "done", "u-3")
    assert steps[3] == ("POST", "/worker/events/delivery", {"event_id": "ev-3", "status": "processed"})
    assert steps[4] == ("PUT", "/worker", "idle")


def test_a_control_request_is_answered_and_marked_done(api):
    worker, _, calls, _ = api
    request = Event.from_wire(_envelope(9, {"type": "control_request", "request_id": "r-9", "request": {"subtype": "set_model"}}))
    worker.respond(request, {"model": "m"}, pending_permission_requests=[])
    worker.reject(request, "no such model", error_code="invalid_model")
    ok, refused = (body["events"][0]["payload"]["response"] for body in _sent(calls, "POST", "/worker/events"))
    assert ok == {"subtype": "success", "request_id": "r-9", "response": {"model": "m"}, "pending_permission_requests": []}
    assert refused == {"subtype": "error", "request_id": "r-9", "error": "no such model", "error_code": "invalid_model"}
    assert _sent(calls, "POST", "/worker/events/delivery")[-1]["updates"] == [{"event_id": "ev-9", "status": "processed"}]


def test_a_permission_prompt_parks_the_session_until_answered(api):
    worker, _, calls, _ = api
    request_id = worker.request_permission(
        "Bash", {"command": "ls"}, tool_use_id="t-1", description="Run: ls", raw_command="ls")
    prompt = _sent(calls, "POST", "/worker/events")[-1]["events"][0]["payload"]
    assert prompt["request_id"] == request_id
    assert prompt["request"] == {
        "subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash", "input": {"command": "ls"},
        "tool_use_id": "t-1", "description": "Run: ls"}
    parked = _sent(calls, "PUT", "/worker")[-1]
    assert parked["worker_status"] == "requires_action"
    assert parked["requires_action_details"] == {
        "tool_name": "Bash", "action_description": "Run: ls", "raw_command": "ls", "request_id": request_id, "tool_use_id": "t-1"}
    assert parked["external_metadata"]["pending_action"]["input"] == {"command": "ls"}
    rule = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "ls"}], "behavior": "allow", "destination": "session"}]
    worker.request_permission("Bash", {"command": "ls"}, tool_use_id="t-2", description="Run: ls", permission_suggestions=rule)
    assert _sent(calls, "POST", "/worker/events")[-1]["events"][0]["payload"]["request"]["permission_suggestions"] == rule
    worker.report_state("running")  # any other state clears the prompt
    resumed = _sent(calls, "PUT", "/worker")[-1]
    assert resumed["requires_action_details"] is None and resumed["external_metadata"] == {"pending_action": None}


def test_report_metadata_sets_keys(api):
    worker, _, calls, _ = api
    worker.report_metadata(model="m", context_usage={"used_tokens": 1, "max_tokens": 2})
    assert _sent(calls, "PUT", "/worker")[-1] == {
        "worker_epoch": 1, "external_metadata": {"model": "m", "context_usage": {"used_tokens": 1, "max_tokens": 2}}}


# --- builders ------------------------------------------------------------------
def test_worker_init_and_initialize_response_carry_the_settings():
    init = worker_init(model="m", cwd="/w", permission_mode="plan", effort="low", slash_commands=["model"])
    assert (init["type"], init["subtype"]) == ("system", "init")
    assert (init["model"], init["cwd"], init["permissionMode"], init["effort"]) == ("m", "/w", "plan", "low")
    assert init["slash_commands"] == ["model"] and init["tools"] == []
    answer = worker_initialize_response(model="m", permission_mode="plan")
    assert (answer["current_model"], answer["current_permission_mode"]) == ("m", "plan")
    assert answer["claude_code_version"] == init["claude_code_version"]


def test_worker_assistant_takes_text_or_blocks():
    said = worker_assistant("hello", model="m", stop_reason="end_turn")
    assert said["message"]["content"] == [{"type": "text", "text": "hello"}]
    assert said["message"]["id"].startswith("msg_") and said["message"]["stop_reason"] == "end_turn"
    call = {"type": "tool_use", "id": "t-1", "name": "Bash", "input": {"command": "ls"}}
    used = worker_assistant([call], model="m", message_id="msg_1")
    assert used["message"]["content"] == [call] and used["message"]["id"] == "msg_1"
    assert Event.from_wire({"payload": used}).tool_uses() == [call]


def test_worker_tool_result_and_stream_event_shapes():
    result = worker_tool_result("t-1", "out", is_error=True)
    assert result["type"] == "user"
    assert result["message"]["content"] == [{"type": "tool_result", "tool_use_id": "t-1", "content": "out", "is_error": True}]
    assert "tool_use_result" not in result
    sent = {"attachments": [{"path": "/w/plot.png", "size": 3, "isImage": True, "file_uuid": "f-1"}]}
    assert worker_tool_result("t-2", "1 file delivered", tool_use_result=sent)["tool_use_result"] == sent
    delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "h"}}
    assert worker_stream_event(delta) == {"type": "stream_event", "event": delta, "parent_tool_use_id": None}


def test_worker_result_ends_a_turn_as_success_or_error():
    ok = worker_result(result="done", user_message_uuid="u-1", terminal_reason="aborted_streaming")
    assert (ok["subtype"], ok["is_error"], ok["stop_reason"]) == ("success", False, "end_turn")
    assert ok["user_message_uuid"] == "u-1" and ok["terminal_reason"] == "aborted_streaming"
    assert Event.from_wire({"payload": ok}).is_turn_end
    failed = worker_result(result="boom", is_error=True)
    assert (failed["subtype"], failed["is_error"], failed["stop_reason"]) == ("error_during_execution", True, None)
    assert "user_message_uuid" not in failed


def test_worker_control_response_and_permission_request_shapes():
    assert worker_control_response("r")["response"] == {"subtype": "success", "request_id": "r", "response": {}}
    prompt = worker_permission_request("r", "Write", {"file_path": "a"}, tool_use_id="t")
    assert prompt["type"] == "control_request" and "description" not in prompt["request"]
    assert "permission_suggestions" not in prompt["request"]
    assert Event.from_wire({"payload": prompt, "source": "worker"}).is_blocking_control
    rule = [{"type": "addRules", "rules": [{"toolName": "Write"}], "behavior": "allow", "destination": "session"}]
    offered = worker_permission_request("r", "Write", {"file_path": "a"}, tool_use_id="t", permission_suggestions=rule)
    assert offered["request"]["permission_suggestions"] == rule


def test_a_compaction_is_a_status_and_a_boundary():
    assert worker_status("compacting") == {"type": "system", "subtype": "status", "status": "compacting"}
    assert worker_status(None)["status"] is None
    assert worker_compact_boundary() == {
        "type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}}
    assert worker_compact_boundary(trigger="manual", pre_tokens=900, post_tokens=100)["compact_metadata"] == {
        "trigger": "manual", "pre_tokens": 900, "post_tokens": 100}


def test_a_reset_names_the_new_conversation_and_the_message_that_asked():
    reset = worker_conversation_reset("conv-2", user_message_uuid="u-1")
    assert reset.pop("timestamp").endswith("Z")
    assert reset == {
        "type": "conversation_reset", "new_conversation_id": "conv-2", "trigger": "clear",
        "user_message_uuid": "u-1", "user_message_from_human": True}
    assert "user_message_uuid" not in worker_conversation_reset("conv-2", trigger="fresh_session")


def test_a_worker_says_why_it_is_leaving():
    assert worker_shutting_down() == {"type": "system", "subtype": "worker_shutting_down", "reason": "host_exit"}


# --- the client's two hosting calls ---------------------------------------------
def test_create_and_bridge_session_requests(monkeypatch):
    rc = RemoteControlClient.__new__(RemoteControlClient)
    rc.base_url = "https://api.example"
    seen = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw.get("json")))
        if url.endswith("/bridge"):
            return httpx.Response(200, json={"worker_jwt": "j", "worker_epoch": "3"})
        return httpx.Response(200, json={"deduplicated": False, "session": {"id": "cse_new", "title": "t"}})

    monkeypatch.setattr(rc, "_request", fake_request)
    assert rc.create_session("t", cwd="/w", model="m", tags=["a:b"]) == {"id": "cse_new", "title": "t"}
    assert seen[0] == ("POST", "https://api.example/v1/code/sessions",
                       {"title": "t", "bridge": {}, "config": {"cwd": "/w", "model": "m"}, "tags": ["a:b"]})
    assert rc.bridge_session("cse_new")["worker_epoch"] == "3"
    assert seen[1] == ("POST", "https://api.example/v1/code/sessions/cse_new/bridge", {})
    rc.bridge_session("cse_new", expected_epoch=3)
    assert seen[2][2] == {"expected_epoch": 3}
    rc.create_session("t", cwd="/w", repository="me/project", branch="main")
    assert seen[3][2]["config"] == {
        "cwd": "/w",
        "sources": [{"type": "git_repository", "url": "https://github.com/me/project", "revision": "main"}],
        "outcomes": [{"type": "git_repository", "git_info": {"type": "github", "repo": "me/project", "branches": []}}]}


def test_an_attached_file_is_downloaded_with_the_login(monkeypatch):
    rc = RemoteControlClient.__new__(RemoteControlClient)
    rc.base_url = "https://api.example"
    seen = []

    def fake_request(method, url, **kw):
        seen.append((method, url))
        return httpx.Response(404, json={"error": {"type": "not_found_error"}}) if "missing" in url else httpx.Response(200, content=b"bytes")

    monkeypatch.setattr(rc, "_request", fake_request)
    assert rc.download_file("a/b") == b"bytes"
    assert seen == [("GET", "https://api.example/api/oauth/files/a%2Fb/content")]  # the id stays one segment
    with pytest.raises(APIError) as too_long:
        rc.download_file("a", max_bytes=3)
    assert too_long.value.status == 413
    with pytest.raises(APIError) as missing:
        rc.download_file("missing")
    assert missing.value.status == 404


def test_a_file_is_uploaded_as_the_one_part_of_a_form(monkeypatch):
    rc = RemoteControlClient.__new__(RemoteControlClient)
    rc.base_url = "https://api.example"
    seen = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw))
        if b"too long" in kw["content"]:
            return httpx.Response(413, json={"error": {"details": {"max_bytes": 3}}})
        return httpx.Response(201, json={"file_uuid": "f-1", "file_kind": "image"})

    monkeypatch.setattr(rc, "_request", fake_request)
    assert rc.upload_file('a "plot".png', b"\x89PNG") == {"file_uuid": "f-1", "file_kind": "image"}
    method, url, kw = seen[0]
    assert (method, url) == ("POST", "https://api.example/api/oauth/file_upload")
    boundary = kw["extra_headers"]["Content-Type"].removeprefix("multipart/form-data; boundary=")
    assert kw["content"] == (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a \\"plot\\".png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + b"\x89PNG" + f"\r\n--{boundary}--\r\n".encode()
    rc.upload_file("notes", b"hi", media_type="text/plain")
    assert b"Content-Type: text/plain\r\n" in seen[1][2]["content"]
    rc.upload_file("notes", b"hi")
    assert b"Content-Type: application/octet-stream\r\n" in seen[2][2]["content"]  # a name that says nothing
    with pytest.raises(APIError) as refused:
        rc.upload_file("big.bin", b"too long")
    assert refused.value.status == 413
