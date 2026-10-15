#!/usr/bin/env python3
"""Example 04: Memory — project memory and conversation store.

Demonstrates persistent memory across sessions.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.memory.project_memory import ProjectMemory
from agent.memory.conversation_store import ConversationStore


def main():
    # Use temp dir for demo
    tmp = tempfile.mkdtemp()

    # 1. Project memory
    print("=" * 50)
    print("Project Memory\n")

    pm = ProjectMemory(base_dir=tmp)
    pm.set("project_name", "My Awesome Project", project="demo")
    pm.set("version", "1.0.0", project="demo")
    pm.set("config", {"debug": True, "timeout": 30}, project="demo")

    print(f"project_name: {pm.get('project_name', project='demo')}")
    print(f"version: {pm.get('version', project='demo')}")
    print(f"config: {pm.get('config', project='demo')}")

    # List all keys
    if hasattr(pm, "keys"):
        print(f"\nAll keys: {pm.keys(project='demo')}")

    # 2. Conversation store
    print("\n" + "=" * 50)
    print("Conversation Store\n")

    store = ConversationStore(base_dir=tmp)
    session = "demo_session"

    store.add_message("user", "Hello, what's the weather?", session)
    store.add_message("assistant", "I don't have weather data.", session)
    store.add_message("user", "OK, tell me a joke instead.", session)
    store.add_message("assistant", "Why did the programmer quit? "
                                   "He didn't get arrays.", session)

    messages = store.get_messages(session)
    print(f"Stored {len(messages)} messages:")
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")[:60]
        print(f"  [{role}]: {content}")

    # 3. Persistence check — reload from disk
    print("\n" + "=" * 50)
    print("Persistence check (reload from disk):\n")

    pm2 = ProjectMemory(base_dir=tmp)
    print(f"Reloaded project_name: "
          f"{pm2.get('project_name', project='demo')}")

    print(f"\nDemo data stored in: {tmp}")


if __name__ == "__main__":
    main()
