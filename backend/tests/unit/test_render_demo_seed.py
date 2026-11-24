import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.seed_render_demo import demo_specs  # noqa: E402


def test_render_seed_is_limited_to_ten_demo_agents_without_external_http_agent():
    specs = demo_specs()

    assert len(specs) == 10
    assert len({spec["slug"] for spec in specs}) == 10
    assert all("DEMO AGENT" in spec["tags"] for spec in specs)
    assert all("entrypoint" not in spec for spec in specs)
