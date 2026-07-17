"""Drive a fully cloud-hosted agent via the public Managed Agents API.

Unlike Remote Control (which steers a session running on your machine), this
creates an agent + session that run in Anthropic's cloud. Needs a real API key:

    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/managed_agents_cloud.py
"""
import os

from claude_rc import ManagedAgentsClient


def main() -> None:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    with ManagedAgentsClient(api_key) as ma:
        # 1. Create a reusable agent (model/system/tools live here, not on the session).
        agent = ma.create_agent(
            name="Example Assistant",
            model="claude-opus-4-8",
            tools=[{"type": "agent_toolset_20260401"}],
        )
        print("agent:", agent["id"])

        # 2. Create a cloud environment for it to run in.
        env = ma.create_environment(
            name="example-env",
            config={"type": "cloud", "networking": {"type": "unrestricted"}},
        )
        print("environment:", env["id"])

        # 3. Start a session bound to the agent + environment.
        session = ma.create_session(agent=agent["id"], environment_id=env["id"])
        sid = session["id"]
        print("session:", sid)

        # 4. Stream first, then send (the stream only delivers events after it opens).
        ma.send_message(sid, "In one sentence, what is a monad?")
        for ev in ma.stream_events(sid):
            if ev.type == "agent.message":
                print("agent>", ev.text())
            if ev.type == "session.status_idle":
                break

        ma.archive_session(sid)


if __name__ == "__main__":
    main()
