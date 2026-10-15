#!/usr/bin/env python3
"""Example 05: API client — interact with the agent via HTTP.

Starts a simple demo of the API endpoints.
Requires the server running: python -m agent.app.cli serve
"""
import os
import sys
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.environ.get("AGENT_API_URL", "http://localhost:8000")
TOKEN = os.environ.get("AGENT_API_TOKEN", "")


def _request(method: str, path: str,
             data: dict = None) -> dict:
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "status": resp.status,
                    "data": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": e.read().decode()[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    print(f"API base: {BASE}\n")

    # 1. Health check (no auth)
    print("1. Health check:")
    r = _request("GET", "/api/health")
    print(f"   {r}\n")

    # 2. List tools
    print("2. List tools:")
    r = _request("GET", "/api/tools")
    if r["ok"]:
        tools = r["data"].get("tools", [])
        print(f"   {len(tools)} tools available")
        for t in tools[:5]:
            print(f"   - {t.get('name')}")
    else:
        print(f"   Error: {r.get('error')}\n")

    # 3. Chat
    print("\n3. Chat:")
    r = _request("POST", "/api/chat",
                 {"message": "Hello, what can you do?",
                  "session_id": "demo"})
    if r["ok"]:
        data = r["data"]
        print(f"   Reply: {data.get('reply', '')[:200]}")
    else:
        print(f"   Error: {r.get('error')}")

    # 4. Create plan
    print("\n4. Create plan:")
    r = _request("POST", "/api/plan",
                 {"goal": "Count Python files"})
    if r["ok"]:
        data = r["data"]
        print(f"   Plan: {data.get('id')}, "
              f"{len(data.get('tasks', {}))} tasks")
    else:
        print(f"   Error: {r.get('error')}")

    # 5. Server stats
    print("\n5. Server stats:")
    r = _request("GET", "/api/stats")
    print(f"   {r}")


if __name__ == "__main__":
    main()
