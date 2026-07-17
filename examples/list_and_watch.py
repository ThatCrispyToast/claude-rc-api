"""List your Remote Control sessions and watch one live (read-only).

    python examples/list_and_watch.py            # list
    python examples/list_and_watch.py <cse_id>   # stream that session
"""
import sys

from claude_rc import RemoteControlClient


def main() -> None:
    with RemoteControlClient() as rc:
        if len(sys.argv) < 2:
            for s in rc.sessions():
                print(f"{s['id']}  {s.get('status'):8}  {s.get('title', '')[:60]}")
            print("\nPass a session id to stream it live.")
            return

        session_id = sys.argv[1]
        print(f"streaming {session_id} (Ctrl-C to stop)…\n")
        for ev in rc.stream_events(session_id):
            if ev.role == "assistant" and ev.text().strip():
                print("claude>", ev.text().strip())
            elif ev.type == "result":
                print(f"— turn complete ({ev.subtype}) —")


if __name__ == "__main__":
    main()
