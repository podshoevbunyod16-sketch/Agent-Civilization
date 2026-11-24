from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.seed_demo import AGENTS, DEMO_AGENT_SLUGS  # noqa: E402


@pytest.mark.parametrize("slug", sorted(DEMO_AGENT_SLUGS))
def test_demo_swarm_has_ten_explicitly_marked_agent_specs(slug: str):
    specs = {agent["slug"]: agent for agent in AGENTS}
    assert len(DEMO_AGENT_SLUGS) == 10
    assert slug in specs
    assert "DEMO AGENT" in specs[slug]["tags"]
    assert specs[slug].get("code")


async def test_tkinter_builder_emits_real_python_source():
    from app.sandbox import SandboxRunner

    builder = next(agent for agent in AGENTS if agent["slug"] == "demo-tkinter-builder")
    result = await SandboxRunner(backend="subprocess").run_python(
        builder["code"],
        {
            "input": {"prompt": "A small daily planner"},
            "depends_on_outputs": {"design": {"spec": {"theme": "dark"}}},
        },
    )
    assert result.status == "succeeded"
    source = result.output["source_code"]
    compile(source, "app.py", "exec")
    assert "root = tk.Tk()" in source
    assert "root.mainloop()" in source
    assert result.output["title"] == "A small daily planner"
