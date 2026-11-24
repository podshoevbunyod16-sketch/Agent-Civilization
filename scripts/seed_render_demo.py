#!/usr/bin/env python3
"""Safely publish the ten demo agents into an already-deployed Render database.

This script does not register an account, create a node, submit tasks, grant admin
privileges, or print credentials. First create a normal account in the deployed UI,
then run this script and enter that account's password at the hidden prompt.

    python scripts/seed_render_demo.py \
        --api https://agent-civilization-1.onrender.com \
        --email you@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from scripts.seed_demo import AGENTS, DEMO_AGENT_SLUGS  # noqa: E402


def demo_specs() -> list[dict]:
    return [spec for spec in AGENTS if spec["slug"] in DEMO_AGENT_SLUGS]


async def seed(api_url: str, email: str, password: str) -> int:
    base_url = api_url.rstrip("/") + "/api/v1"
    created_count = 0
    already_present = 0

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        login = await client.post("/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        account_id = login.json()["user"]["id"]
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        for spec in demo_specs():
            payload = {
                "slug": spec["slug"],
                "name": spec["name"],
                "summary": spec["summary"],
                "description": spec["description"],
                "category": spec["category"],
                "tags": sorted({*spec["tags"], "DEMO AGENT"}),
                "accent_color": spec["accent"],
                "capabilities": [
                    {"capability_key": key, "declared_level": level}
                    for key, level in spec["capabilities"]
                ],
            }
            response = await client.post("/agents", json=payload)
            if response.status_code == 409:
                existing = await client.get(f"/agents/by-slug/{spec['slug']}")
                existing.raise_for_status()
                agent = existing.json()
                if agent["owner"]["id"] != account_id:
                    raise RuntimeError(
                        f"Slug {spec['slug']} belongs to a different account; "
                        "stop and resolve ownership before retrying."
                    )
                if agent["status"] == "published" and agent["current_version"] > 0:
                    print(f"already published: {spec['slug']}")
                    already_present += 1
                    continue
            else:
                response.raise_for_status()
                agent = response.json()

            version = await client.post(
                f"/agents/{agent['id']}/versions",
                json={
                    "changelog": "Published deterministic investor-demo template",
                    "entrypoint_type": "python",
                    "entrypoint_config": {
                        "code": spec["code"],
                        "runtime": "python3.12",
                    },
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "required_capabilities": [
                        "demo.provider",
                        *(["research.web_search"] if spec["slug"] == "researcher-prime" else []),
                        *(
                            ["python.syntax_check"]
                            if spec["slug"]
                            in {
                                "demo-tkinter-builder",
                                "demo-tkinter-reviewer",
                                "demo-tkinter-test-designer",
                            }
                            else []
                        ),
                    ],
                    "resource_limits": {
                        "max_cpu_seconds": 10,
                        "max_wall_seconds": 30,
                        "max_memory_mb": 256,
                        "allow_network": False,
                        "max_output_bytes": 65536,
                    },
                    "publish": True,
                },
            )
            version.raise_for_status()
            print(f"published: {spec['slug']}")
            created_count += 1

    print(f"Done: {created_count} published, {already_present} already present.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="Public Render base URL (without /api/v1)")
    parser.add_argument(
        "--email", required=True, help="Email of an account already registered in the UI"
    )
    args = parser.parse_args()
    password = getpass.getpass("Account password (input hidden): ")
    if not password:
        parser.error("Password cannot be empty")
    return asyncio.run(seed(args.api, args.email, password))


if __name__ == "__main__":
    raise SystemExit(main())
