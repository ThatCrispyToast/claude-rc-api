"""Advertise a Remote Control session that isn't Claude Code: an echo bot.

Run it, then open claude.ai/code or the Claude app. The session is listed under
Remote Control, and whatever you send it comes straight back. Ctrl-C to stop.

    python examples/echo_worker.py                 # a new session
    python examples/echo_worker.py cse_…           # serve an existing one again

Requires a claude.ai login in ~/.claude/.credentials.json. The session appears
in your own account only. A session can't be deleted: archive it when done, in
the app or with `RemoteControlClient().archive_session(id)`.
"""
import os
import sys

from claude_rc import (
    RemoteControlClient,
    RemoteControlWorker,
    worker_assistant,
    worker_init,
    worker_initialize_response,
)

MODEL = "echo"


def main() -> None:
    rc = RemoteControlClient()
    session_id = sys.argv[1] if len(sys.argv) > 1 else rc.create_session("echo worker")["id"]
    print(f"serving {session_id}: https://claude.ai/code/{session_id.replace('cse_', 'session_', 1)}")

    init = worker_init(model=MODEL, cwd=os.getcwd())
    with RemoteControlWorker(rc, session_id) as worker:
        worker.send(init)
        for ev in worker.events():
            if ev.type == "user":
                print(f"> {ev.text()}")
                worker.begin_turn(ev)
                worker.send(worker_assistant(f"echo: {ev.text()}", model=MODEL, stop_reason="end_turn"))
                worker.end_turn(ev)
            elif ev.control_subtype == "initialize":  # every controller sends it as it attaches
                worker.respond(ev, worker_initialize_response(model=MODEL))
                worker.send(init)
            elif ev.type == "control_request":
                worker.reject(ev, f"the echo worker does not handle {ev.control_subtype}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
