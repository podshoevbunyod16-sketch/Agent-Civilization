"""Render-friendly worker for trusted deterministic demo agents.

Unlike the general-purpose node worker, this process never executes Python source
from the agent registry and never invokes a shell. It supports only the built-in
allow-list in ``demo_provider`` and trusted worker-side tools.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Any

from app.core.config import settings
from app.worker.demo_provider import DEMO_AGENT_SLUGS, run_demo_agent
from app.worker.node_worker import NodeClient, NodeWorker
from app.worker.tools.web_search import WebSearchUnavailable, web_search

logger = logging.getLogger("demo-worker")


def _check_python_syntax(source: str) -> dict[str, Any]:
    try:
        compile(source, "app.py", "exec")
    except SyntaxError as exc:
        return {"status": "failed", "line": exc.lineno, "message": exc.msg}
    return {"status": "passed"}


class DemoNodeWorker(NodeWorker):
    """Claim real API tasks but execute only fixed, reviewed demo handlers."""

    async def execute(self, assignment: dict[str, Any]) -> dict[str, Any]:
        slug = str(assignment.get("agent_slug", ""))
        if slug not in DEMO_AGENT_SLUGS:
            return {
                "success": False,
                "error_message": "Demo Provider refuses agents outside its built-in allow-list.",
                "metrics": {"backend": "demo-provider", "status": "unsupported_agent"},
            }

        payload = dict(assignment.get("input_payload") or {})
        tool_metrics: dict[str, Any] = {"backend": "demo-provider", "provider": "deterministic"}
        tool_calls: list[dict[str, Any]] = []

        if slug == "researcher-prime":
            if assignment.get("requester_id") != assignment.get("agent_owner_id"):
                return {
                    "success": False,
                    "error_message": "DEMO web_search доступен только владельцу seed-агента.",
                    "metrics": {"tool": "web_search", "status": "forbidden"},
                }
            await self.report_progress(
                str(assignment.get("task_id", "")),
                str(assignment.get("lease_token", "")),
                "tool_call: web_search",
            )
            try:
                results = await web_search(
                    str(payload.get("query", "")), api_key=settings.tavily_api_key
                )
            except WebSearchUnavailable as exc:
                return {
                    "success": False,
                    "error_message": str(exc),
                    "metrics": {"tool": "web_search", "status": "failed"},
                }
            payload["search_results"] = results
            tool_metrics.update(
                {"tool": "web_search", "status": "succeeded", "result_count": len(results)}
            )
            tool_calls.append(
                {"name": "web_search", "status": "succeeded", "result_count": len(results)}
            )

        if slug in {
            "demo-tkinter-builder",
            "demo-tkinter-reviewer",
            "demo-tkinter-test-designer",
        }:
            await self.report_progress(
                str(assignment.get("task_id", "")),
                str(assignment.get("lease_token", "")),
                "tool_call: python.syntax_check (compile only; source is not executed)",
            )
            dependencies = payload.get("depends_on_outputs") or {}
            build = dependencies.get("build") or {}
            source = str(build.get("source_code", ""))
            if slug == "demo-tkinter-builder":
                # The generated Python is data; compile validates syntax without executing it.
                generated = run_demo_agent(slug, payload)
                source = str(generated.get("source_code", ""))
                result = {**generated, "syntax_check": _check_python_syntax(source)}
            else:
                result = run_demo_agent(slug, payload)
                result["syntax_check"] = _check_python_syntax(source)
            tool_metrics.update(
                {"tool": "python.syntax_check", "status": result["syntax_check"]["status"]}
            )
            tool_calls.append(
                {
                    "name": "python.syntax_check",
                    **result["syntax_check"],
                }
            )
        else:
            result = run_demo_agent(slug, payload)

        return {
            "success": True,
            "output": {
                **result,
                "demo_provider": "deterministic",
                "tool_calls": tool_calls,
            },
            "metrics": tool_metrics,
        }


async def main_async(args: argparse.Namespace) -> int:
    if not args.node_key or not args.node_secret:
        raise SystemExit("Set AC_NODE_KEY and AC_NODE_SECRET in the worker environment.")
    client = NodeClient(args.api_url, args.node_key, args.node_secret)
    worker = DemoNodeWorker(
        client,
        capabilities=["demo.provider", "research.web_search", "python.syntax_check"],
        poll_interval=args.poll_interval,
        max_tasks=args.max_tasks,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await worker.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("AC_API_URL", ""))
    parser.add_argument("--node-key", default=os.environ.get("AC_NODE_KEY"))
    parser.add_argument("--node-secret", default=os.environ.get("AC_NODE_SECRET"))
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-tasks", type=int, default=4)
    args = parser.parse_args()
    if not args.api_url:
        parser.error("Set AC_API_URL to the public API URL")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
