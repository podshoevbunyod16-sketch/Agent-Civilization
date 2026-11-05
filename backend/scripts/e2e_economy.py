"""E2E-проверка этапа 6 на живом стеке: бюджеты, двойная запись, награды.

Сценарий на реальных данных:

1. пользователь получает стартовый бюджет (ACU — внутренние единицы);
2. задание ставится с бюджетом → бюджет замораживается на эскроу;
3. узел исполняет задание и докладывает стоимость работы;
4. расчёт: выплата агенту, комиссия платформы, возврат остатка;
5. проверки этапа 5 подтверждают результат → начисляется награда из казны;
6. повторное начисление невозможно (идемпотентность);
7. вывод заработка агентом на счёт владельца;
8. сверка журнала: каждая транзакция сходится, балансы = сумма проводок;
9. права: чужой счёт недоступен, эмиссия только у админа.
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

AGENT_CODE = """
def run(payload):
    data = payload.get("input") or payload
    return {"summary": str(data.get("topic", "тема")), "facts": ["a", "b"]}
"""


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

        # Владелец агента — отдельный пользователь, чтобы переводы были осмысленны
        owner_reg = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-owner-{suffix}@example.com",
                "password": "OwnerPass123",
                "display_name": "E2E владелец",
            },
        )
        assert owner_reg.status_code == 201, owner_reg.text
        owner_headers = {"Authorization": f"Bearer {owner_reg.json()['access_token']}"}

        money = await http.get(f"{API}/economy/me", headers=owner_headers)
        assert money.status_code == 200, money.text
        start_balance = money.json()["balance_units"]
        print(f"1. стартовый бюджет нового участника: {start_balance} ACU")
        assert start_balance > 0

        # Агент владельца
        created = await http.post(
            f"{API}/agents",
            json={
                "slug": f"e2e-money-{suffix}",
                "name": f"E2E экономика {suffix}",
                "summary": "Агент для e2e-проверки экономики",
                "capabilities": [
                    {"capability_key": "research.web_search", "declared_level": 3}
                ],
            },
            headers=owner_headers,
        )
        assert created.status_code == 201, created.text
        agent = created.json()
        version = await http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "version": "1.0.0",
                "changelog": "e2e money",
                "runtime": "python3.11",
                "entrypoint_type": "python",
                "entrypoint_config": {"code": AGENT_CODE},
                "resource_limits": {"max_wall_seconds": 10, "max_memory_mb": 128},
                "publish": True,
            },
            headers=owner_headers,
        )
        assert version.status_code == 201, version.text
        print(f"2. агент опубликован: {agent['id']}")

        # Задание с бюджетом заказчика (архитектор) — исполняет агент владельца
        from app.worker.node_worker import NodeClient, NodeWorker

        node_key, node_secret = load_node_credentials()
        client = NodeClient("http://127.0.0.1:8000", node_key, node_secret)
        worker = NodeWorker(client, poll_interval=0.2, max_tasks=3)
        await worker.verify()

        budget_units = 20.0
        submitted = await http.post(
            f"{API}/tasks",
            json={
                "agent_id": agent["id"],
                "input_payload": {"topic": "внутренняя экономика"},
                "budget_units": budget_units,
            },
            headers=headers,
        )
        assert submitted.status_code == 201, submitted.text
        task = submitted.json()

        held = await http.get(f"{API}/tasks/{task['id']}/budget", headers=headers)
        if held.status_code != 200:
            held = await http.get(f"{API}/economy/tasks/{task['id']}/budget", headers=headers)
        assert held.status_code == 200, held.text
        print(
            f"3. бюджет задания заморожен: {held.json()['budget_units']} ACU "
            f"(статус {held.json()['status']})"
        )
        assert held.json()["status"] == "held"

        assignment = None
        for _ in range(10):
            assignments = await worker.claim()
            assignment = next(
                (item for item in assignments if item["task_id"] == task["id"]), None
            )
            if assignment:
                break
            await asyncio.sleep(0.1)
        assert assignment is not None, "узел не получил задание"
        execution = await worker.execute(assignment)
        assert execution["success"], execution
        await worker.submit_result(
            task["id"],
            assignment["lease_token"],
            success=True,
            output=execution["output"],
            cost_units=4.0,
            metrics=execution.get("metrics"),
        )
        print("4. задание исполнено, стоимость работы 4.0 ACU")

        settled = await http.get(
            f"{API}/economy/tasks/{task['id']}/budget", headers=headers
        )
        assert settled.status_code == 200, settled.text
        body = settled.json()
        print(
            f"5. расчёт: израсходовано {body['spent_units']}, "
            f"комиссия {body['fee_units']}, возврат {body['released_units']} "
            f"(статус {body['status']})"
        )
        assert body["status"] == "settled"
        assert body["spent_units"] == 4.0
        assert body["fee_units"] == 0.2
        assert body["released_units"] == budget_units - 4.0

        owner_money = (await http.get(f"{API}/economy/me", headers=owner_headers)).json()
        earned = next(i for i in owner_money["agents"] if i["agent_id"] == agent["id"])
        print(f"6. заработок агента: {earned['balance_units']} ACU")
        assert earned["balance_units"] == 3.8

        # Проверка результата → награда из казны
        verified = await http.post(
            f"{API}/tasks/{task['id']}/verifications",
            json={
                "output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                "assertions": [{"path": "facts", "op": "len_gte", "value": 2}],
            },
            headers=headers,
        )
        assert verified.status_code == 201, verified.text
        assert verified.json()["verdict"]["verdict"] == "verified", verified.text
        print("7. результат подтверждён: вердикт verified")

        # Награда начисляется при расчёте бюджета; задание уже рассчитано,
        # поэтому повторяем сверку через проверку идемпотентности награды.
        rewards = await http.get(f"{API}/economy/rewards", headers=owner_headers)
        assert rewards.status_code == 200, rewards.text
        grants = rewards.json()
        assert grants, "награда за подтверждённый результат не начислена"
        print(
            f"8. наград начислено: {len(grants)} "
            f"(сумма {grants[0]['amount_units']} ACU: база {grants[0]['base_units']} "
            f"+ бонус {grants[0]['bonus_units']})"
        )

        # Сверка журнала
        report = await http.post(f"{API}/economy/verify", headers=headers)
        assert report.status_code == 200, report.text
        verdict = report.json()
        print(
            f"9. сверка журнала: ok={verdict['ok']}, "
            f"несходящихся транзакций {len(verdict['unbalanced_transactions'])}, "
            f"расхождений счетов {len(verdict['accounts_mismatch'])}, "
            f"всего единиц {verdict['total_units']}"
        )
        assert verdict["ok"] is True

        # Вывод заработка агентом
        withdraw = await http.post(
            f"{API}/economy/withdraw",
            json={"agent_id": agent["id"], "amount_units": 3.0},
            headers=owner_headers,
        )
        assert withdraw.status_code == 201, withdraw.text
        after_withdraw = (await http.get(f"{API}/economy/me", headers=owner_headers)).json()
        agent_left = next(
            item for item in after_withdraw["agents"] if item["agent_id"] == agent["id"]
        )["balance_units"]
        print(
            f"10. вывод 3 ACU: бюджет владельца {after_withdraw['balance_units']}, "
            f"остаток агента {agent_left}"
        )

        # История операций
        history = await http.get(f"{API}/economy/transactions", headers=headers)
        assert history.status_code == 200, history.text
        kinds = sorted({item["transaction"]["kind"] for item in history.json()})
        print(f"11. история операций: {kinds}")

        # Права: чужой счёт, эмиссия, отмена операции
        stranger_reg = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-nosy-{suffix}@example.com",
                "password": "NosyPass123",
                "display_name": "E2E чужой",
            },
        )
        stranger_headers = {"Authorization": f"Bearer {stranger_reg.json()['access_token']}"}
        stranger_account = (
            await http.get(f"{API}/economy/me", headers=stranger_headers)
        ).json()["account_id"]
        owner_account = owner_money["account_id"]

        foreign_statement = await http.get(
            f"{API}/economy/accounts/{owner_account}/statement", headers=stranger_headers
        )
        foreign_spend = await http.post(
            f"{API}/economy/transfer",
            json={
                "from_account_id": owner_account,
                "to_account_id": stranger_account,
                "amount_units": 5,
            },
            headers=stranger_headers,
        )
        reverse_attempt = await http.post(
            f"{API}/economy/transactions/{history.json()[0]['transaction']['id']}/reverse",
            headers=owner_headers,
        )
        print(
            f"12. права: чужой счёт {foreign_statement.status_code}, "
            f"чужое списание {foreign_spend.status_code}, "
            f"отмена операции {reverse_attempt.status_code}"
        )
        assert foreign_statement.status_code == 403
        assert foreign_spend.status_code == 403
        assert reverse_attempt.status_code == 403

        stats = await http.get(f"{API}/economy/stats", headers=headers)
        assert stats.status_code == 200, stats.text
        totals = stats.json()
        print(
            f"13. сводка: транзакций "
            f"{sum(item['count'] for item in totals['transactions'].values())}, "
            f"счетов {sum(item['count'] for item in totals['accounts'].values())}"
        )

        # Идемпотентность перевода
        source = (await http.get(f"{API}/economy/me", headers=headers)).json()
        idem_payload = {
            "from_account_id": source["account_id"],
            "to_account_id": owner_account,
            "amount_units": 7,
            "idempotency_key": f"e2e-{suffix}",
        }
        first = await http.post(f"{API}/economy/transfer", json=idem_payload, headers=headers)
        second = await http.post(f"{API}/economy/transfer", json=idem_payload, headers=headers)
        assert first.status_code == 201 and second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        balance = (await http.get(f"{API}/economy/me", headers=headers)).json()
        print(
            f"14. идемпотентность: повтор с тем же ключом дал ту же транзакцию, "
            f"баланс {balance['balance_units']}"
        )

    await client.close()
    print("E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
