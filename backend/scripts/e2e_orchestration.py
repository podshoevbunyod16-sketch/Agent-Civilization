"""E2E-проверка этапа 4 на живом стеке: план из шагов выполняется реальными агентами.

Сценарий:
1. два python-агента публикуются (аналитик и суммаризатор);
2. создаётся план analyze → summarize с зависимостями;
3. план запускается, узел-воркер забирает задания и исполняет их в песочнице;
4. проверяются: порядок шагов, передача результатов зависимостей,
   прогресс плана, события, повторный запуск (идемпотентность) и права.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

# Скрипт запускается из backend/, но на всякий случай добавляем корень пакета
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://127.0.0.1:8000/api/v1"
LOGIN = {"email": "architect@agent-civilization.dev", "password": "Architect2026"}

def load_node_credentials() -> tuple[str, str]:
    """Читает учётные данные узла, созданные `scripts/seed_demo.py`."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "var" / "seed-node-credentials.json"
    if not path.exists():
        raise SystemExit(
            "Нет var/seed-node-credentials.json — сначала запустите scripts/seed_demo.py"
        )
    data = json.loads(path.read_text())
    return data["node_key"], data["node_secret"]


ANALYZE_CODE = """
def run(payload):
    topic = payload.get("plan", {}).get("goal", {}).get("topic", "неизвестно")
    return {"facts": [f"факт о {topic}", "второй факт"], "source": "analyze"}
"""

SUMMARIZE_CODE = """
def run(payload):
    deps = payload.get("depends_on_outputs", {})
    facts = deps.get("analyze", {}).get("facts", [])
    return {"summary": " / ".join(facts)[:120], "fact_count": len(facts)}
"""


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        login = await http.post(f"{API}/auth/login", json=LOGIN)
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        suffix = uuid.uuid4().hex[:6]

        agents = {}
        for slug, name, code, caps in (
            ("e2e-analyst", "E2E аналитик", ANALYZE_CODE, ["research.web_search"]),
            ("e2e-writer", "E2E суммаризатор", SUMMARIZE_CODE, ["text.summarize"]),
        ):
            created = await http.post(
                f"{API}/agents",
                json={
                    "slug": f"{slug}-{suffix}",
                    "name": name,
                    "summary": "Агент для e2e-проверки оркестрации",
                    "capabilities": [{"capability_key": key, "declared_level": 3} for key in caps],
                },
                headers=headers,
            )
            created.raise_for_status()
            agent = created.json()
            version = await http.post(
                f"{API}/agents/{agent['id']}/versions",
                json={
                    "version": "1.0.0",
                    "changelog": "e2e",
                    "runtime": "python3.11",
                    "entrypoint_type": "python",
                    "entrypoint_config": {"code": code},
                    "resource_limits": {"max_wall_seconds": 10, "max_memory_mb": 128},
                    "publish": True,
                },
                headers=headers,
            )
            assert version.status_code == 201, version.text
            agents[slug] = agent
        print("1. агенты опубликованы")

        plan = await http.post(
            f"{API}/plans",
            json={
                "title": f"E2E план {suffix}",
                "goal": {"topic": "агентные платформы"},
                "auto_start": False,
                "max_parallelism": 2,
                "steps": [
                    {
                        "key": "analyze",
                        "name": "Собрать факты",
                        "required_capabilities": ["research.web_search"],
                        # Явный исполнитель: e2e проверяет оркестрацию,
                        # а не скоринг обнаружения (он покрыт тестами).
                        "agent_id": agents["e2e-analyst"]["id"],
                    },
                    {
                        "key": "summarize",
                        "name": "Суммировать",
                        "required_capabilities": ["text.summarize"],
                        "depends_on": ["analyze"],
                        "agent_id": agents["e2e-writer"]["id"],
                    },
                ],
            },
            headers=headers,
        )
        assert plan.status_code == 201, plan.text
        plan_body = plan.json()
        plan_id = plan_body["id"]
        steps = {step["key"]: step for step in plan_body["steps"]}
        print(
            f"2. план создан: {plan_id}, анализ назначен на {steps['analyze']['agent_id']}, "
            f"score={steps['analyze']['match_score']}"
        )
        assert steps["analyze"]["status"] == "ready"
        assert steps["summarize"]["status"] == "pending"

        # Реальный узел: подписанные запросы как у настоящего worker
        from app.worker.node_worker import NodeClient, NodeWorker

        node_key, node_secret = load_node_credentials()
        worker = NodeWorker(
            NodeClient("http://127.0.0.1:8000", node_key, node_secret),
            poll_interval=0.2,
            max_tasks=2,
        )
        print("3. узел-воркер готов")

        async def drain(stage: str, rounds: int = 3) -> None:
            """Несколько итераций воркера: забрать задания и отчитаться."""
            for _ in range(rounds):
                processed = await worker.run_once()  # возвращает число заданий
                if processed:
                    print(f"   {stage}: воркер обработал {processed} заданий")
                await asyncio.sleep(0.05)

        run1 = await http.post(f"{API}/plans/{plan_id}/run", headers=headers)
        assert run1.status_code == 200, run1.text
        print(f"4. запуск: {run1.json()['launched']} (статус {run1.json()['status']})")
        await drain("analyze")

        progress = await http.get(f"{API}/plans/{plan_id}/progress", headers=headers)
        print(f"5. прогресс: {json.dumps(progress.json()['by_status'], ensure_ascii=False)}")
        assert progress.json()["by_status"]["succeeded"] == 1

        run2 = await http.post(f"{API}/plans/{plan_id}/run", headers=headers)
        assert run2.status_code == 200, run2.text
        print(f"6. следующий шаг: {run2.json()['launched']}")
        assert run2.json()["launched"] == ["summarize"]
        await drain("summarize")

        final = await http.post(f"{API}/plans/{plan_id}/run", headers=headers)
        assert final.status_code == 200, final.text
        body = final.json()
        print(f"7. финал: статус {body['status']}, готово {body['completed']}")

        detail = await http.get(f"{API}/plans/{plan_id}", headers=headers)
        detail.raise_for_status()
        steps = {step["key"]: step for step in detail.json()["steps"]}
        summary = steps["summarize"]["output_payload"]
        print(
            "8. суммаризатор получил результаты зависимости: "
            f"{json.dumps(summary, ensure_ascii=False)}"
        )
        snapshot = steps["summarize"]["resolved_input"].get("depends_on_outputs")
        print(f"   snapshot входа: {json.dumps(snapshot, ensure_ascii=False)}")
        assert summary["fact_count"] == 2, summary
        assert snapshot["analyze"]["source"] == "analyze"

        again = await http.post(f"{API}/plans/{plan_id}/run", headers=headers)
        assert again.status_code == 409
        again_code = again.json()["error"]["code"]
        print(f"9. повторный запуск завершённого плана: {again.status_code} {again_code}")

        events = await http.get(f"{API}/plans/{plan_id}/events", headers=headers)
        types = [event["event_type"] for event in events.json()]
        print(f"10. событий плана: {len(types)}; типы: {sorted(set(types))}")
        assert "plan.succeeded" in types

        # изоляция: чужой пользователь не видит план
        other = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-plan-{suffix}@example.com",
                "password": "Str0ngPassw0rd!",
                "display_name": "E2E чужой",
            },
        )
        other.raise_for_status()
        foreign = await http.get(
            f"{API}/plans/{plan_id}",
            headers={"Authorization": f"Bearer {other.json()['access_token']}"},
        )
        assert foreign.status_code == 403, foreign.text
        print(f"11. доступ чужого пользователя к плану: {foreign.status_code}")

        stats = await http.get(f"{API}/plans/stats", headers=headers)
        print(f"12. статистика: {json.dumps(stats.json(), ensure_ascii=False)}")

        print("\nE2E OK: DAG-план выполнен реальными агентами, зависимости и права работают.")


if __name__ == "__main__":
    asyncio.run(main())
