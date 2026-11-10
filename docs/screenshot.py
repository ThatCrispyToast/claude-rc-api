"""Regenerate the README's screenshots of the web UI from made-up sessions.

    uv run --with playwright python docs/screenshot.py

It serves the real page over a stub client, so it needs no network and no
login, and captures it in both colour schemes as ``docs/webui-{light,dark}.png``.
The clock is frozen, so a rerun gives the same picture. Playwright uses its own
Chromium (``playwright install chromium``) unless ``CHROMIUM`` names a browser.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from claude_rc.events import Event
from claude_rc.webui import _Handler, _RCServer

NOW = datetime(2026, 11, 10, 16, 42, tzinfo=timezone.utc)
OUT = Path(__file__).parent
SELECTED = "cse_01JBd3kq8YbNn2Wf6sJdQe7R"


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def session(sid, title, model, effort, used, worker="idle", detail="", needs="", pinned=False,
            offline=False, minutes=1, unread=False):
    return {
        "id": sid, "title": title, "status": "active", "worker_status": worker,
        "connection_status": "disconnected" if offline else "connected",
        "tags": ["ui:pinned"] if pinned else [], "unread": unread,
        "last_event_at": ago(minutes), "config": {"model": model},
        "session_url": f"https://claude.ai/code/{sid}",
        "external_metadata": {
            "model": model, "effort_level": effort, "container_cc_version": "2.1.322",
            "context_usage": {"used_tokens": used, "max_tokens": 1_000_000},
            "post_turn_summary": {"status_detail": detail, "needs_action": needs},
        },
    }


SESSIONS = [
    session(SELECTED, "Retry uploads on transient S3 errors", "claude-opus-5-5", "high", 61_400,
            worker="requires_action", needs="Allow Bash to run the uploader tests", pinned=True),
    session("cse_01JBc9Vt2mPq4Lx8Hn5Rw3Ks", "Release notes for v2.4", "claude-sonnet-5-5", "medium",
            23_800, detail="Drafted the CHANGELOG entry; ready for review", pinned=True, minutes=38),
    session("cse_01JBdA7wQe1Zr6Ty3Uo9Pi2M", "Move the settings page to the new form API",
            "claude-fable-5-2", "xhigh", 412_000, worker="running",
            detail="Porting the profile form to useForm validation", minutes=0, unread=True),
    session("cse_01JBcFm4Nb8Xc2Vz5Lk7Jh1G", "Flaky checkout e2e test", "claude-opus-5-5", "max",
            187_300, detail="Found the race in the cart fixture; fix is in checkout.spec.ts",
            minutes=94),
    session("cse_01JBbS6Dg3Ha9Jw1Ke4Lr8Tq", "Dependency audit", "claude-sonnet-5-5", "low", 9_200,
            detail="Two advisories, both dev-only; no upgrades needed", offline=True, minutes=60 * 26),
]

_seq = 0


def wire(minutes: float, payload: dict, source: str = "worker") -> dict:
    global _seq
    _seq += 1
    return {"sequence_num": _seq, "event_id": f"evt_{_seq}", "source": source,
            "created_at": ago(minutes), "payload": payload}


def said(minutes, role, text="", tools=()):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": f"tu_{i}", "name": n, "input": inp}
                for i, (n, inp) in enumerate(tools)]
    return wire(minutes, {"type": role, "message": {"role": role, "content": content}},
                "client" if role == "user" else "worker")


TRANSCRIPT = [
    wire(24, {"type": "system", "subtype": "init", "model": "claude-opus-5-5",
              "permissionMode": "default", "effort": "high"}),
    said(23, "user", "What does the uploader do when S3 answers with a 503?"),
    said(23, "assistant", tools=[("Grep", {"pattern": "put_object"}),
                                 ("Read", {"file_path": "src/uploader/client.py"})]),
    said(22, "assistant", "It gives up. `Uploader.put` makes one `put_object` call and lets "
                          "`ClientError` propagate, so a single 503 fails the whole batch."),
    wire(22, {"type": "result", "subtype": "success"}),
    said(19, "user", "Add retries with exponential backoff, and cover them with a test."),
    said(18, "assistant", "I'll wrap the call in a small backoff helper:\n\n"
                          "- retry on 429, 5xx and connection resets, nothing else\n"
                          "- full jitter, 0.5 s base and an 8 s cap, five attempts\n"
                          "- re-raise the last error, so callers still see the real failure",
         tools=[("Write", {"file_path": "src/uploader/retry.py"}),
                ("Edit", {"file_path": "src/uploader/client.py"}),
                ("Edit", {"file_path": "tests/test_upload.py"})]),
    said(15, "assistant", "Added `retry.py` and wired it into `Uploader.put`. The two new tests "
                          "drive a fake clock, so they finish instantly. Running them now."),
    wire(15, {"type": "control_request", "request_id": "req_1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "tu_bash",
        "input": {"command": "uv run pytest -q tests/test_upload.py"}}}),
]


class DemoClient:
    """Just enough of RemoteControlClient for the page."""

    def sessions(self, limit=20, statuses=None):
        return SESSIONS[:limit]

    def get_session(self, sid):
        return next(s for s in SESSIONS if s["id"] == sid)

    def list_events(self, sid, limit=200, sort_order="desc"):
        return [Event.from_wire(e) for e in reversed(TRANSCRIPT)][:limit] if sid == SELECTED else []

    def stream_events(self, sid, **_):
        threading.Event().wait()  # a quiet, open stream: the page shows "live"
        yield from ()


class DemoHandler(_Handler):
    def _whoami(self):
        return {"logged_in": True, "expired": False, "subscription": "max", "org_uuid_present": True}


def main() -> None:
    httpd = _RCServer(("127.0.0.1", 0), DemoHandler, DemoClient(), verbose=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM"))
        for scheme in ("light", "dark"):
            ctx = browser.new_context(viewport={"width": 1280, "height": 880}, device_scale_factor=2,
                                      color_scheme=scheme, timezone_id="UTC", locale="en-US")
            page = ctx.new_page()
            page.clock.set_fixed_time(NOW)
            page.goto(url)
            page.click(f'[title="{SELECTED}"]')
            page.wait_for_selector(".prompt .acts button")
            page.wait_for_function("document.getElementById('stream-state').textContent === 'live'")
            page.screenshot(path=OUT / f"webui-{scheme}.png")
            ctx.close()
        browser.close()
    httpd.shutdown()


if __name__ == "__main__":
    main()
