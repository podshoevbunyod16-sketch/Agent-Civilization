"""E2E-проверка этапа 5 на живом стеке: верификация, репутация, отзывы, апелляции.

Сценарий полностью на реальных данных — без подставных вердиктов:
1. публикуется python-агент, задание реально исполняется узлом-воркером;
2. автоматические проверки (схема + утверждения + тест-код в песочнице)
   подтверждают результат → вердикт `verified`, репутация растёт;
3. второе задание с испорченным результатом проваливает проверку →
   вердикт `rejected`, репутация падает;
4. возможности агента: заявленный уровень против подтверждённого;
5. отзыв владельца задания (5★) влияет на рейтинг;
6. апелляция на отклонённый результат: админ удовлетворяет → вклад
   отзывается, вердикт пересчитывается;
7. сканирование аномалий находит повторное использование результата;
8. сводка доверия и проверка прав (чужой пользователь → 403).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://127.0.0.1:8000/api/v1"
LOGIN = {"email": "architect@agent-civilization.dev", "password": "Architect2026"}

# Агент возвращает тему входа и фиксированный набор фактов
AGENT_CODE = """
def run(payload):
    data = payload.get("input") or payload
    return {
        "summary": str(data.get("topic", "тема")),
        "facts": ["факт 1", "факт 2"],
        "confidence": 0.93,
    }
"""

CHECK_CONFIG = {
    "output_schema": {
        "type": "object",
        "required": ["summary", "facts"],
        "properties": {
            "summary": {"type": "string", "minLength": 1},
            "facts": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
    "assertions": [
        {"path": "facts", "op": "len_gte", "value": 2},
        {"path": "confidence", "op": "gte", "value": 0.5},
    ],
    "test_code": (
        "def run(payload):\n"
        "    output = payload['output']\n"
        "    return {'passed': output.get('confidence', 0) > 0.9}\n"
    ),
}


def load_node_credentials() -> tuple[str, str]:
    path = Path(__file__).resolve().parents[2] / "var" / "seed-node-credentials.json"
    if not path.exists():
        raise SystemExit(
            "Нет var/seed-node-credentials.json — сначала запустите scripts/seed_demo.py"
        )
    data = json.loads(path.read_text())
    return data["node_key"], data["node_secret"]


async def main() -> None:
    async with httpx.AsyncClient(timeout=60) as http:
        login = await http.post(f"{API}/auth/login", json=LOGIN)
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        suffix = uuid.uuid4().hex[:6]

        # 0. Агент с заявленной возможностью
        created = await http.post(
            f"{API}/agents",
            json={
                "slug": f"e2e-trust-{suffix}",
                "name": f"E2E доверие {suffix}",
                "summary": "Агент для e2e-проверки верификации",
                "capabilities": [
                    {"capability_key": "research.web_search", "declared_level": 4}
                ],
            },
            headers=headers,
        )
        created.raise_for_status()
        agent = created.json()
        version = await http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "version": "1.0.0",
                "changelog": "e2e trust",
                "runtime": "python3.11",
                "entrypoint_type": "python",
                "entrypoint_config": {"code": AGENT_CODE},
                "resource_limits": {"max_wall_seconds": 10, "max_memory_mb": 128},
                "publish": True,
            },
            headers=headers,
        )
        assert version.status_code == 201, version.text
        print(f"1. агент опубликован: {agent['id']}")

        # Реальный воркер узла: подписанные запросы как у настоящего worker
        from app.worker.node_worker import NodeClient, NodeWorker

        node_key, node_secret = load_node_credentials()
        client = NodeClient("http://127.0.0.1:8000", node_key, node_secret)
        worker = NodeWorker(client, poll_interval=0.2, max_tasks=3)
        await worker.verify()

        async def run_task(topic: str, *, output_override: dict | None = None) -> dict:
            submitted = await http.post(
                f"{API}/tasks",
                json={"agent_id": agent["id"], "input_payload": {"topic": topic}},
                headers=headers,
            )
            assert submitted.status_code == 201, submitted.text
            task = submitted.json()
            assignment = None
            for _ in range(10):
                assignments = await worker.claim()
                assignment = next(
                    (item for item in assignments if item["task_id"] == task["id"]), None
                )
                if assignment is not None:
                    break
                await asyncio.sleep(0.1)
            assert assignment is not None, "узел не получил задание"

            if output_override is None:
                # Настоящее исполнение python-кода агента в песочнице
                execution = await worker.execute(assignment)
                await worker.submit_result(
                    task["id"],
                    assignment["lease_token"],
                    success=execution["success"],
                    output=execution["output"],
                    error_message=execution.get("error_message"),
                    metrics=execution.get("metrics"),
                )
            else:
                # Испорченный результат: узел докладывает заведомо неверный output
                await worker.submit_result(
                    task["id"],
                    assignment["lease_token"],
                    success=True,
                    output=output_override,
                    metrics={"duration_ms": 5},
                )
            detail = await http.get(f"{API}/tasks/{task['id']}", headers=headers)
            assert detail.status_code == 200, detail.text
            body = detail.json()
            assert body["status"] == "succeeded", body
            return body

        # 2. Задание, которое проходит все проверки
        good = await run_task("агентные платформы")
        print(f"2. задание выполнено: {good['id']}, hash={good['result_hash'][:12]}…")

        checks = await http.post(
            f"{API}/tasks/{good['id']}/verifications", json=CHECK_CONFIG, headers=headers
        )
        assert checks.status_code == 201, checks.text
        body = checks.json()
        methods = {check["method"]: check["status"] for check in body["checks"]}
        print(f"3. проверки: {methods} → вердикт {body['verdict']['verdict']}")
        assert methods == {"schema": "passed", "assertions": "passed", "auto_test": "passed"}
        assert body["verdict"]["verdict"] == "verified"

        rep_after_good = (
            await http.get(f"{API}/agents/{agent['id']}/reputation", headers=headers)
        ).json()
        print(
            "4. репутация: "
            f"{rep_after_good['reputation_score']} "
            f"(источники: {sorted(rep_after_good['by_source'])})"
        )
        assert rep_after_good["by_source"]["verification_passed"]["count"] == 1
        assert rep_after_good["verified_results"] == 1

        # 3. Задание с испорченным результатом
        bad = await run_task(
            "испорченное задание",
            output_override={"summary": 12345, "facts": "не массив"},
        )
        failed = await http.post(
            f"{API}/tasks/{bad['id']}/verifications",
            json={"output_schema": CHECK_CONFIG["output_schema"]},
            headers=headers,
        )
        assert failed.status_code == 201, failed.text
        failed_body = failed.json()
        print(
            f"5. испорченный результат: {failed_body['checks'][0]['status']} → "
            f"вердикт {failed_body['verdict']['verdict']}"
        )
        assert failed_body["verdict"]["verdict"] == "rejected"

        rep_after_bad = (
            await http.get(f"{API}/agents/{agent['id']}/reputation", headers=headers)
        ).json()
        print(
            f"6. репутация после отказа: {rep_after_bad['reputation_score']} "
            f"(отрицательные вклады {sorted(rep_after_bad['by_source'])})"
        )
        assert rep_after_bad["by_source"]["verification_failed"]["count"] == 1
        assert rep_after_bad["reputation_score"] < rep_after_good["reputation_score"]

        # 4. Заявленное против подтверждённого
        trust = await http.get(f"{API}/agents/{agent['id']}/trust", headers=headers)
        assert trust.status_code == 200, trust.text
        trust_body = trust.json()
        capability = trust_body["capabilities"][0]
        print(
            f"7. возможность {capability['key']}: заявлено {capability['declared_level']}, "
            f"подтверждено {capability['verified_level']} (trust_level={capability['trust_level']})"
        )
        # Подтверждённых заданий 1 из 2 (50%) — порог 80% не пройден
        assert capability["verified_level"] is None
        assert capability["trust_level"] == "declared"
        assert trust_body["verdicts"] == {
            "unverified": 0,
            "verified": 1,
            "rejected": 1,
            "disputed": 0,
        }

        # 5. Отзыв владельца задания
        review = await http.post(
            f"{API}/tasks/{good['id']}/review",
            json={"rating": 5, "comment": "Результат подтверждён", "confirm_result": True},
            headers=headers,
        )
        assert review.status_code == 201, review.text
        rating = await http.get(f"{API}/agents/{agent['id']}/rating", headers=headers)
        print(
            f"8. отзыв: {review.json()['rating']}/5, средняя {rating.json()['average_rating']}, "
            f"подтверждённых результатов {rating.json()['confirmed_results']}"
        )
        assert rating.json()["reviews"] == 1
        assert rating.json()["confirmed_results"] == 1

        # 6. Апелляция на отклонённый результат (нужен админ)
        admin_login = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-admin-{suffix}@example.com",
                "password": "AdminPass123",
                "display_name": "E2E админ",
            },
        )
        assert admin_login.status_code == 201, admin_login.text
        admin_id = admin_login.json()["user"]["id"]
        await promote_to_admin(admin_id)
        admin_login = await http.post(
            f"{API}/auth/login",
            json={"email": f"e2e-admin-{suffix}@example.com", "password": "AdminPass123"},
        )
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

        appeal = await http.post(
            f"{API}/appeals",
            json={
                "agent_id": agent["id"],
                "task_id": bad["id"],
                "verification_id": failed_body["checks"][0]["id"],
                "reason": "Контракт задания допускал числовой summary — проверка ошибочна",
                "evidence": {"contract_ref": "agent v1.0.0", "note": "e2e"},
            },
            headers=headers,
        )
        assert appeal.status_code == 201, appeal.text
        print(f"9. апелляция открыта: {appeal.json()['id']}")

        resolved = await http.post(
            f"{API}/appeals/{appeal.json()['id']}/resolve",
            json={"accept": True, "resolution": "Проверка отозвана, результат принят"},
            headers=admin_headers,
        )
        assert resolved.status_code == 200, resolved.text
        print(f"10. апелляция удовлетворена: {resolved.json()['status']}")

        rep_final = (
            await http.get(f"{API}/agents/{agent['id']}/reputation", headers=headers)
        ).json()
        verdicts = await http.get(f"{API}/tasks/{bad['id']}/verdict", headers=headers)
        print(
            f"11. после апелляции: репутация {rep_final['reputation_score']}, "
            f"источники {sorted(rep_final['by_source'])}, вердикт {verdicts.json()['verdict']}"
        )
        assert "verification_failed" not in rep_final["by_source"]
        assert rep_final["by_source"]["appeal"]["count"] == 1
        assert rep_final["reputation_score"] > rep_after_bad["reputation_score"]
        assert verdicts.json()["verdict"] == "unverified"

        # 7. Аномалии: три одинаковых результата на разные входы
        for topic in ("один", "два", "три"):
            await run_task(
                topic,
                output_override={"summary": "копия", "facts": ["a", "b"], "confidence": 0.95},
            )
        scan = await http.post(
            f"{API}/agents/{agent['id']}/anomalies/scan", headers=headers
        )
        assert scan.status_code == 200, scan.text
        found = scan.json()["found"]
        print(
            f"12. аномалии: {[(item['anomaly_type'], item['severity']) for item in found]}"
        )
        assert any(item["anomaly_type"] == "result_reuse" for item in found)

        dismissed = await http.post(
            f"{API}/anomalies/{found[0]['id']}/resolve",
            json={"confirm": False},
            headers=admin_headers,
        )
        assert dismissed.status_code == 200, dismissed.text
        print(f"13. ложный сигнал отклонён: {dismissed.json()['status']}")

        # 8. Права: чужой пользователь не видит проверки и не сканирует
        stranger = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-stranger-{suffix}@example.com",
                "password": "StrangerPass123",
                "display_name": "Чужой",
            },
        )
        assert stranger.status_code == 201, stranger.text
        stranger_headers = {
            "Authorization": f"Bearer {stranger.json()['access_token']}"
        }
        foreign_scan = await http.post(
            f"{API}/agents/{agent['id']}/anomalies/scan", headers=stranger_headers
        )
        foreign_checks = await http.post(
            f"{API}/tasks/{good['id']}/verifications",
            json=CHECK_CONFIG,
            headers=stranger_headers,
        )
        own_appeals = await http.get(f"{API}/appeals", headers=stranger_headers)
        print(
            f"14. права: скан {foreign_scan.status_code}, проверка {foreign_checks.status_code}, "
            f"чужих апелляций {len(own_appeals.json())}"
        )
        assert foreign_scan.status_code == 403
        assert foreign_checks.status_code == 403
        assert own_appeals.json() == []

        overview = await http.get(f"{API}/trust/overview", headers=headers)
        assert overview.status_code == 200, overview.text
        summary = overview.json()
        print(
            f"15. сводка: вердикты {summary['verdicts']}, "
            f"аномалий открыто {summary['open_anomalies']}, "
            f"апелляций открыто {summary['open_appeals']}"
        )
        assert summary["checks"]["schema"]["passed"] >= 1
        assert summary["checks"]["auto_test"]["passed"] >= 1

    await client.close()
    print("E2E OK")


async def promote_to_admin(user_id: str) -> None:
    """Повышает пользователя до админа напрямую в БД (роль не выдаётся по API)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": user_id}
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
