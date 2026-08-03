"""Send a message to a live Remote Control session and print the reply.

    python examples/send_message.py <cse_id> "run the tests and summarize failures"

This steers a real session — only run it against a session you started with
`claude remote-control`.
"""
import sys

from claude_rc import RemoteControlClient


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: send_message.py <session_id> <message>")
    session_id, message = sys.argv[1], sys.argv[2]

    with RemoteControlClient() as rc:
        # send_and_collect sends, then streams from the sequence number that was
        # current beforehand (so nothing is missed), and returns every event
        # until the turn completes (a `result` event) or blocks on you.
        for ev in rc.send_and_collect(session_id, message):
            if ev.role == "assistant" and ev.text().strip():
                print("claude>", ev.text().strip())
            for tu in ev.tool_uses():
                print("   · tool:", tu.get("name"))
            if ev.type == "result":
                print(f"\n[done: {ev.subtype}]")


if __name__ == "__main__":
    main()
