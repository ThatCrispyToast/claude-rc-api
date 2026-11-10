"""The web panel's HTTP surface, served for real on a loopback port over a stub client."""
import json
import threading
import urllib.request

import pytest

from claude_rc.events import Event
from claude_rc.webui import _Handler, _RCServer, event_to_dict


class _StubRC:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kw):
            self.calls.append((name, args, kw))
            return {"sessions": [{"id": "cse_a"}], "pin_session": {"tags": ["ui:pinned"]},
                    "unpin_session": {"tags": []}}.get(name)
        return call


@pytest.fixture
def panel():
    rc = _StubRC()
    httpd = _RCServer(("127.0.0.1", 0), _Handler, rc, verbose=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", data=data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    yield rc, call
    httpd.shutdown()
    httpd.server_close()


def test_session_list_is_active_unless_asked(panel):
    rc, call = panel
    assert call("/api/sessions") == {"sessions": [{"id": "cse_a"}]}
    call("/api/sessions?all=1")
    assert [kw["statuses"] for _, _, kw in rc.calls] == ["active", None]


def test_pin_archive_and_mark_read_routes(panel):
    rc, call = panel
    assert call("/api/sessions/cse_a/pin", {"pinned": True}) == {"ok": True, "tags": ["ui:pinned"]}
    assert call("/api/sessions/cse_a/pin", {"pinned": False})["tags"] == []
    call("/api/sessions/cse_a/archive", {})
    call("/api/sessions/cse_a/unarchive", {})
    call("/api/sessions/cse_a/mark_read", {"event_id": "e-1"})
    assert [(name, args) for name, args, _ in rc.calls] == [
        ("pin_session", ("cse_a",)), ("unpin_session", ("cse_a",)), ("archive_session", ("cse_a",)),
        ("unarchive_session", ("cse_a",)), ("mark_read", ("cse_a", "e-1")),
    ]


def test_event_json_says_when_a_prompt_is_settled():
    withdrawn = Event.from_wire({"source": "worker", "payload": {"type": "control_cancel_request", "request_id": "p1"}})
    assert {k: event_to_dict(withdrawn)[k] for k in ("settles_prompt", "request_id")} == {
        "settles_prompt": True, "request_id": "p1"}
