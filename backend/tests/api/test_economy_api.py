"""API-тесты внутренней экономики: двойная запись, бюджеты, награды (этап 6).

Главные проверки — не «эндпоинт вернул 200», а инварианты:

* сумма проводок каждой транзакции равна нулю;
* нельзя потратить больше, чем есть (кроме казначейства);
* повтор операции с тем же ключом не создаёт второго начисления;
* история не удаляется: отмена — обратная транзакция;
* кэш балансов совпадает с суммой проводок (`/economy/verify`).
"""

from __future__ import annotations

import uuid

from app.services.economy import SIGNUP_GRANT_UNITS
from tests.conftest import create_published_agent, register_node_client


async def _finished_task(client, user, agent: dict, *, budget: float | None = None) -> dict:
    payload: dict = {
        "agent_id": agent["id"],
        "input_payload": {"topic": "экономика"},
    }
    if budget is not None:
        payload["budget_units"] = budget
    submitted = await client.post("/api/v1/tasks", json=payload, headers=user.headers)
    assert submitted.status_code == 201, submitted.text
    task = submitted.json()
    _node, node_client = await register_node_client(client, user)
    claimed = await node_client.request(
        "POST", "/api/v1/nodes/me/tasks/claim", json_body={"limit": 5}
    )
    assert claimed.status_code == 200, claimed.text
    assignment = next(
        item for item in claimed.json()["assignments"] if item["task_id"] == task["id"]
    )
    reported = await node_client.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{task['id']}/result",
        json_body={
            "lease_token": assignment["lease_token"],
            "success": True,
            "output": {"summary": "готово", "facts": ["a", "b"]},
            "cost_units": 4.0,
            "metrics": {"duration_ms": 10},
        },
    )
    assert reported.status_code == 200, reported.text
    return task


class TestAccounts:
    async def test_signup_grant_creates_account(self, client, user):
        response = await client.get("/api/v1/economy/me", headers=user.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["currency"] == "ACU"
        assert body["balance_units"] == SIGNUP_GRANT_UNITS

    async def test_signup_grant_is_idempotent(self, client, user):
        await client.get("/api/v1/economy/me", headers=user.headers)
        again = await client.get("/api/v1/economy/me", headers=user.headers)
        assert again.json()["balance_units"] == SIGNUP_GRANT_UNITS

    async def test_accounts_list_scoped(self, client, user, other_user):
        mine = await client.get("/api/v1/economy/accounts", headers=user.headers)
        assert mine.status_code == 200
        types = {account["type"] for account in mine.json()}
        assert types == {"user"}
        # Чужих счетов не видно
        foreign = await client.get("/api/v1/economy/accounts", headers=other_user.headers)
        assert all(account["type"] != "treasury" for account in foreign.json())

    async def test_treasury_hidden_from_non_admin(self, client, user, admin_user):
        as_user = await client.get("/api/v1/economy/accounts", headers=user.headers)
        assert "treasury" not in {account["type"] for account in as_user.json()}
        as_admin = await client.get("/api/v1/economy/accounts", headers=admin_user.headers)
        assert as_admin.status_code == 200

    async def test_foreign_statement_forbidden(self, client, user, other_user, session):
        from sqlalchemy import select

        from app.db.models.economy import Account

        account = (
            await session.execute(
                select(Account).where(Account.owner_id == uuid.UUID(user.id))
            )
        ).scalars().first()
        assert account is not None
        response = await client.get(
            f"/api/v1/economy/accounts/{account.id}/statement", headers=other_user.headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "account_forbidden"

    async def test_statement_lists_entries(self, client, user):
        me = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        response = await client.get(
            f"/api/v1/economy/accounts/{me['account_id']}/statement", headers=user.headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_in"] == SIGNUP_GRANT_UNITS
        assert body["entries"][0]["kind"] == "deposit"
        assert body["account"]["balance_units"] == SIGNUP_GRANT_UNITS

    async def test_unknown_account(self, client, user):
        response = await client.get(
            f"/api/v1/economy/accounts/{uuid.uuid4()}/statement", headers=user.headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "account_not_found"


class TestTransfer:
    async def test_transfer_between_accounts(self, client, user, other_user):
        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()

        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": source["account_id"],
                "to_account_id": target["account_id"],
                "amount_units": 30,
                "comment": "оплата консультации",
            },
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "transfer"

        after_source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        after_target = (
            await client.get("/api/v1/economy/me", headers=other_user.headers)
        ).json()
        assert after_source["balance_units"] == SIGNUP_GRANT_UNITS - 30
        assert after_target["balance_units"] == SIGNUP_GRANT_UNITS + 30

    async def test_insufficient_funds(self, client, user, other_user):
        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()
        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": source["account_id"],
                "to_account_id": target["account_id"],
                "amount_units": SIGNUP_GRANT_UNITS + 1,
            },
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_funds"

    async def test_cannot_spend_foreign_account(self, client, user, other_user):
        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()
        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": target["account_id"],
                "to_account_id": source["account_id"],
                "amount_units": 5,
            },
            headers=user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "account_forbidden"

    async def test_self_transfer_rejected(self, client, user):
        me = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": me["account_id"],
                "to_account_id": me["account_id"],
                "amount_units": 1,
            },
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "transfer_same_account"

    async def test_negative_amount_validation(self, client, user, other_user):
        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()
        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": source["account_id"],
                "to_account_id": target["account_id"],
                "amount_units": -5,
            },
            headers=user.headers,
        )
        assert response.status_code == 422

    async def test_idempotent_transfer(self, client, user, other_user):
        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()
        payload = {
            "from_account_id": source["account_id"],
            "to_account_id": target["account_id"],
            "amount_units": 10,
            "idempotency_key": "e2e-transfer-1",
        }
        first = await client.post("/api/v1/economy/transfer", json=payload, headers=user.headers)
        second = await client.post("/api/v1/economy/transfer", json=payload, headers=user.headers)
        assert first.status_code == 201 and second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        balance = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        assert balance["balance_units"] == SIGNUP_GRANT_UNITS - 10

    async def test_non_admin_cannot_emit(self, client, user, admin_user, session):
        from sqlalchemy import select

        from app.db.models.economy import Account, AccountType

        treasury = (
            await session.execute(
                select(Account).where(Account.type == AccountType.treasury)
            )
        ).scalars().first()
        assert treasury is not None
        mine = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        response = await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": str(treasury.id),
                "to_account_id": mine["account_id"],
                "amount_units": 1000,
            },
            headers=user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "treasury_forbidden"


class TestTaskBudget:
    async def test_budget_held_on_submit(self, client, user):
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent, budget=20)
        budget = await client.get(
            f"/api/v1/economy/tasks/{task['id']}/budget", headers=user.headers
        )
        assert budget.status_code == 200, budget.text
        body = budget.json()
        assert body["budget_units"] == 20
        assert body["status"] == "settled"
        # Работа стоила 4 единицы: выплата 3.8, комиссия 0.2, возврат 16
        assert body["spent_units"] == 4
        assert body["fee_units"] == 0.2
        assert body["released_units"] == 16

    async def test_budget_insufficient_blocks_submit(self, client, user):
        agent = await create_published_agent(client, user)
        response = await client.post(
            "/api/v1/tasks",
            json={"agent_id": agent["id"], "budget_units": SIGNUP_GRANT_UNITS + 50},
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_funds"

    async def test_no_budget_task_has_no_budget(self, client, user):
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.get(
            f"/api/v1/economy/tasks/{task['id']}/budget", headers=user.headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "budget_not_found"

    async def test_foreign_budget_forbidden(self, client, user, other_user):
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent, budget=10)
        response = await client.get(
            f"/api/v1/economy/tasks/{task['id']}/budget", headers=other_user.headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "task_forbidden"

    async def test_cancel_releases_budget(self, client, user):
        agent = await create_published_agent(client, user)
        submitted = await client.post(
            "/api/v1/tasks",
            json={"agent_id": agent["id"], "budget_units": 25},
            headers=user.headers,
        )
        task = submitted.json()
        balance_after_hold = (
            await client.get("/api/v1/economy/me", headers=user.headers)
        ).json()["balance_units"]
        assert balance_after_hold == SIGNUP_GRANT_UNITS - 25

        cancelled = await client.post(
            f"/api/v1/tasks/{task['id']}/cancel",
            json={"reason": "передумал"},
            headers=user.headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        balance_after_cancel = (
            await client.get("/api/v1/economy/me", headers=user.headers)
        ).json()["balance_units"]
        assert balance_after_cancel == SIGNUP_GRANT_UNITS

        budget = await client.get(
            f"/api/v1/economy/tasks/{task['id']}/budget", headers=user.headers
        )
        assert budget.json()["status"] == "released"

    async def test_agent_earns_from_budget(self, client, user):
        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        money = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        earned = next(
            item for item in money["agents"] if item["agent_id"] == agent["id"]
        )
        assert earned["balance_units"] == 3.8


class TestRewards:
    async def test_verified_result_is_rewarded(self, client, user, session):
        """Награда начисляется только за подтверждённый результат (этап 5 → 6)."""
        from app.db.models.economy import RewardRule

        rule = RewardRule(name="default", verified_reward_units=2.0)
        session.add(rule)
        await session.commit()

        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent)
        # До проверки награды нет
        rewards = await client.get("/api/v1/economy/rewards", headers=user.headers)
        assert rewards.json() == []

        verified = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={
                "output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                }
            },
            headers=user.headers,
        )
        assert verified.status_code == 201, verified.text

        # Начисление происходит при расчёте бюджета; для задания без бюджета
        # вызываем расчёт явно через повторную проверку (идемпотентно).
        from sqlalchemy import select

        from app.db.models.task import Task
        from app.services import economy as economy_service

        db_task = (
            await session.execute(select(Task).where(Task.id == uuid.UUID(task["id"])))
        ).scalars().unique().one()
        grant = await economy_service.grant_reward(session, db_task)
        await session.commit()
        assert grant is not None
        assert grant.amount_units == 2.0

        rewards = await client.get("/api/v1/economy/rewards", headers=user.headers)
        assert len(rewards.json()) == 1
        assert rewards.json()[0]["amount_units"] == 2.0

    async def test_reward_not_duplicated(self, client, user, session):
        from sqlalchemy import select

        from app.db.models.economy import RewardRule
        from app.db.models.task import Task
        from app.services import economy as economy_service

        session.add(RewardRule(name="default", verified_reward_units=1.5))
        await session.commit()
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"assertions": [{"path": "summary", "op": "exists"}]},
            headers=user.headers,
        )
        db_task = (
            await session.execute(select(Task).where(Task.id == uuid.UUID(task["id"])))
        ).scalars().unique().one()
        first = await economy_service.grant_reward(session, db_task)
        second = await economy_service.grant_reward(session, db_task)
        await session.commit()
        assert first is not None and second is not None
        assert first.id == second.id
        grants = await client.get("/api/v1/economy/rewards", headers=user.headers)
        assert len(grants.json()) == 1

    async def test_rule_update_admin_only(self, client, user, admin_user, session):
        from app.db.models.economy import RewardRule

        rule = RewardRule(name="default")
        session.add(rule)
        await session.commit()
        rule_id = str(rule.id)

        denied = await client.patch(
            f"/api/v1/economy/rules/{rule_id}",
            json={"verified_reward_units": 5},
            headers=user.headers,
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "rule_forbidden"

        allowed = await client.patch(
            f"/api/v1/economy/rules/{rule_id}",
            json={"verified_reward_units": 5, "platform_fee_ratio": 0.1},
            headers=admin_user.headers,
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["verified_reward_units"] == 5
        assert allowed.json()["platform_fee_ratio"] == 0.1

    async def test_low_reputation_blocks_reward(self, client, user, session):
        from sqlalchemy import select, update

        from app.db.models.agent import Agent as AgentModel
        from app.db.models.economy import RewardRule
        from app.db.models.task import Task
        from app.services import economy as economy_service

        session.add(RewardRule(name="default", min_reputation=99.0))
        await session.commit()
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"assertions": [{"path": "summary", "op": "exists"}]},
            headers=user.headers,
        )
        db_task = (
            await session.execute(select(Task).where(Task.id == uuid.UUID(task["id"])))
        ).scalars().unique().one()
        await session.execute(
            update(AgentModel)
            .where(AgentModel.id == uuid.UUID(agent["id"]))
            .values(reputation_score=40.0)
        )
        await session.commit()
        grant = await economy_service.grant_reward(session, db_task)
        assert grant is None

    async def test_daily_cap_blocks_reward(self, client, user, session):
        from sqlalchemy import select

        from app.db.models.economy import RewardRule
        from app.db.models.task import Task
        from app.services import economy as economy_service

        session.add(RewardRule(name="default", verified_reward_units=5, daily_cap_units=1))
        await session.commit()
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"assertions": [{"path": "summary", "op": "exists"}]},
            headers=user.headers,
        )
        db_task = (
            await session.execute(select(Task).where(Task.id == uuid.UUID(task["id"])))
        ).scalars().unique().one()
        grant = await economy_service.grant_reward(session, db_task)
        await session.commit()
        # Награда ограничена дневным лимитом, а не полной ставкой
        assert grant is not None
        assert grant.amount_units == 1.0


class TestWithdrawal:
    async def test_owner_withdraws_earnings(self, client, user):
        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        response = await client.post(
            "/api/v1/economy/withdraw",
            json={"agent_id": agent["id"], "amount_units": 3.0},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        money = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        # Бюджет 20: работа стоила 4, остаток 16 вернулся заказчику.
        # Итог: 100 − 4 (фактическая стоимость) + 3 (вывод) = 99
        assert money["balance_units"] == SIGNUP_GRANT_UNITS - 4 + 3.0
        earned = next(i for i in money["agents"] if i["agent_id"] == agent["id"])
        assert earned["balance_units"] == 0.8

    async def test_withdraw_more_than_earned(self, client, user):
        agent = await create_published_agent(client, user)
        response = await client.post(
            "/api/v1/economy/withdraw",
            json={"agent_id": agent["id"], "amount_units": 5},
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_funds"

    async def test_foreign_agent_withdraw_forbidden(self, client, user, other_user):
        agent = await create_published_agent(client, user)
        response = await client.post(
            "/api/v1/economy/withdraw",
            json={"agent_id": agent["id"], "amount_units": 1},
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "agent_forbidden"


class TestLedgerIntegrity:
    async def test_verify_reports_ok(self, client, user):
        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        response = await client.post("/api/v1/economy/verify", headers=user.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["unbalanced_transactions"] == []
        assert body["accounts_mismatch"] == []

    async def test_double_entry_balanced_everywhere(self, client, user, session):
        from sqlalchemy import func, select

        from app.db.models.economy import LedgerEntry

        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        unbalanced = (
            await session.execute(
                select(LedgerEntry.transaction_id, func.sum(LedgerEntry.amount_units))
                .group_by(LedgerEntry.transaction_id)
                .having(func.abs(func.sum(LedgerEntry.amount_units)) > 1e-6)
            )
        ).all()
        assert unbalanced == []
        total = (
            await session.execute(select(func.sum(LedgerEntry.amount_units)))
        ).scalar_one()
        assert abs(float(total or 0)) < 1e-6

    async def test_transactions_history(self, client, user):
        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        response = await client.get("/api/v1/economy/transactions", headers=user.headers)
        assert response.status_code == 200, response.text
        kinds = {item["transaction"]["kind"] for item in response.json()}
        assert {"deposit", "hold", "payout"} <= kinds
        # Влияние на счета владельца (включая счёт его агента): депозит +100,
        # заморозка −20, выплата агенту +3.8, возврат +16 → 99.8.
        # Чистая стоимость работы — 4.0, из них 0.2 комиссия платформы.
        total = sum(item["impact"] for item in response.json())
        assert abs(total - (SIGNUP_GRANT_UNITS - 20 + 3.8 + 16)) < 1e-6
        settle = next(
            item["transaction"]
            for item in response.json()
            if item["transaction"]["kind"] == "payout"
        )
        assert settle["amount_units"] == 4.0
        assert settle["details"]["payout"] == 3.8
        assert settle["details"]["fee"] == 0.2

    async def test_reversal_restores_balance(self, client, user, admin_user):
        agent = await create_published_agent(client, user)
        task = await _finished_task(client, user, agent, budget=20)
        budget = await client.get(
            f"/api/v1/economy/tasks/{task['id']}/budget", headers=user.headers
        )
        assert budget.json()["status"] == "settled"

        history = await client.get(
            "/api/v1/economy/transactions", headers=admin_user.headers
        )
        settle = next(
            item["transaction"]
            for item in history.json()
            if item["transaction"]["kind"] == "payout"
        )
        reversed_tx = await client.post(
            f"/api/v1/economy/transactions/{settle['id']}/reverse?reason=тестовая отмена",
            headers=admin_user.headers,
        )
        assert reversed_tx.status_code == 200, reversed_tx.text
        assert reversed_tx.json()["kind"] == "reversal"

        # Повторная отмена невозможна
        again = await client.post(
            f"/api/v1/economy/transactions/{settle['id']}/reverse",
            headers=admin_user.headers,
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "transaction_already_reversed"

        report = await client.post("/api/v1/economy/verify", headers=admin_user.headers)
        assert report.json()["ok"] is True

    async def test_only_admin_reverses(self, client, user):
        response = await client.post(
            f"/api/v1/economy/transactions/{uuid.uuid4()}/reverse", headers=user.headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "transaction_forbidden"

    async def test_stats(self, client, user):
        agent = await create_published_agent(client, user)
        await _finished_task(client, user, agent, budget=20)
        response = await client.get("/api/v1/economy/stats", headers=user.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["currency"] == "ACU"
        assert body["transactions"]["deposit"]["count"] >= 1
        # Объём расчётной транзакции — вся израсходованная сумма задания,
        # распределение (выплата/комиссия/возврат) видно в details.
        assert body["transactions"]["payout"]["volume"] == 4.0

    async def test_economy_requires_auth(self, client):
        for path in ("/api/v1/economy/me", "/api/v1/economy/transactions"):
            response = await client.get(path)
            assert response.status_code == 401

    async def test_critical_actions_audited(self, client, user, other_user, session):
        from sqlalchemy import select

        from app.db.models.audit import AuditLog

        source = (await client.get("/api/v1/economy/me", headers=user.headers)).json()
        target = (await client.get("/api/v1/economy/me", headers=other_user.headers)).json()
        await client.post(
            "/api/v1/economy/transfer",
            json={
                "from_account_id": source["account_id"],
                "to_account_id": target["account_id"],
                "amount_units": 5,
            },
            headers=user.headers,
        )
        actions = set((await session.execute(select(AuditLog.action))).scalars().all())
        assert "economy.transfer" in actions
