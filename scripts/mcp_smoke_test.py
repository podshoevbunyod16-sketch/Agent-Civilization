#!/usr/bin/env python3
"""Exercise the root MCP endpoint using the project's reusable MCP HTTP client.

Install backend deps (`pip install -e backend`) and pass a dedicated test API key:
  python scripts/mcp_smoke_test.py --url http://localhost:8000/mcp --api-key ac_live_...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.sandbox.mcp import McpClient


async def run(url: str, api_key: str | None) -> int:
    async with McpClient(url, auth_token=api_key) as client:
        initialized = await client.initialize()
        tools = await client.list_tools()
        print("initialize:")
        print(json.dumps(initialized, indent=2, ensure_ascii=False))
        print(f"tools/list: {len(tools)} tools")
        print(json.dumps([tool["name"] for tool in tools], indent=2, ensure_ascii=False))
        if not api_key:
            print("Set --api-key to test authenticated list_agents/create_task calls.")
            return 2
        listed = await client.call_tool_as_json("list_agents", {"limit": 5})
        if listed["is_error"]:
            raise RuntimeError(f"list_agents failed: {listed['output']}")
        payload = listed["output"]
        agents = payload.get("agents", []) if isinstance(payload, dict) else []
        if not agents:
            raise RuntimeError("No published agents found; run scripts/seed_demo.py first.")
        agent = agents[0]
        capability_keys = {item.get("capability_key") for item in agent.get("capabilities", [])}
        if "data.analysis" in capability_keys:
            task_input = {"values": [3, 4, 5, 9]}
        elif "code.review" in capability_keys:
            task_input = {"code": "print('safe demo')"}
        else:
            task_input = {"query": "MCP smoke test"}
        created = await client.call_tool_as_json(
            "create_task", {"agent_id": agent["id"], "input": task_input}
        )
        if created["is_error"]:
            raise RuntimeError(f"create_task failed: {created['output']}")
        print("list_agents:")
        print(json.dumps({"id": agent["id"], "name": agent["name"]}, indent=2, ensure_ascii=False))
        print("create_task:")
        print(json.dumps(created["output"], indent=2, ensure_ascii=False))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--api-key", default=None, help="Bearer key; avoid putting secrets in shell history in shared terminals")
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.api_key))


if __name__ == "__main__":
    raise SystemExit(main())
