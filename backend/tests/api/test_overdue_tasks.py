"""Этап 10, G/H: просроченное задание не залипает в очереди.

Дефект найден chaos-тестом (нагрузка + «узел умер»), а не чтением кода:
задание с истёкшим `deadline_at` проверялось только внутри `claim_tasks`,
то есть при наличии живого узла. Если подходящего узла нет (или он
отвалился), задание оставалось в `queued` навсегда: очередь росла,
`queue_oldest_age_seconds` не падал, а бюджет оставался замороженным.

Тест воспроизводит именно эту ситуацию: задание с прошедшим дедлайном и
без узлов, затем проход обслуживания.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db.models.task import Task, TaskStatus
from app.services import gateway
from tests.conftest import create_published_agent


async def _make_overdue_task(session, client, user, *, budget: float | None = None) -> Task:
    agent = await create_published_agent(client, user)
    created = await client.post(
        "/api/v1/tasks",
        headers=user.headers,
        json={
            "agent_id": agent["id"],
            "input_payload": {"input": {"text": "просрочка"}},
            "budget_units": budget,
            "timeout_seconds": 5,
        },
    )
    assert created.status_code == 201, created.text
    task_id = uuid.UUID(created.json()["id"])
    # Дедлайн в прошлом: имитируем «пока ждали узел, время вышло».
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()
    return (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()


async def test_overdue_task_is_closed_by_maintenance(session, client, user):
    task = await _make_overdue_task(session, client, user)
    assert task.status is TaskStatus.queued

    closed = await gateway.expire_overdue_tasks(session)
    await session.commit()

    assert closed >= 1
    refreshed = (
        await session.execute(select(Task).where(Task.id == task.id))
    ).scalar_one()
    assert refreshed.status is TaskStatus.expired
    assert refreshed.finished_at is not None
    assert refreshed.error["type"] == "DeadlineExpired"


async def test_overdue_task_releases_frozen_budget(session, client, user):
    """Задание не исполнялось — деньги возвращаются заказчику."""
    from app.db.models.economy import Account, AccountType
    from app.services import economy

    task = await _make_overdue_task(session, client, user, budget=5.0)
    await session.refresh(task)
    account = (
        await session.execute(
            select(Account).where(
                Account.owner_id == uuid.UUID(user.id), Account.type == AccountType.user
            )
        )
    ).scalar_one()
    balance_after_hold = account.balance_units

    await gateway.expire_overdue_tasks(session)
    await session.commit()

    refreshed = (
        await session.execute(
            select(Account).where(Account.id == account.id)
        )
    ).scalar_one()
    assert refreshed.balance_units == balance_after_hold + 5.0
    # Журнал остаётся сходящимся: сумма всех балансов — ноль.
    ledger = await economy.verify_ledger(session)
    assert ledger["ok"] is True
    assert ledger["total_units"] == 0


async def test_maintenance_endpoint_reports_overdue_closed(session, client, admin_user):
    """Админ видит результат прохода, а не только число возвращённых аренд."""
    owner = await _register_other(client, admin_user)
    await _make_overdue_task(session, client, owner)

    response = await client.post("/api/v1/ops/maintenance", headers=admin_user.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overdue_closed"] >= 1


async def test_overdue_is_idempotent(session, client, user):
    await _make_overdue_task(session, client, user)
    first = await gateway.expire_overdue_tasks(session)
    await session.commit()
    second = await gateway.expire_overdue_tasks(session)
    await session.commit()
    assert first >= 1
    assert second == 0


async def _register_other(client, admin_user):
    """Второй участник: проход админа закрывает и чужие просроченные задания."""
    from tests.conftest import register_user

    return await register_user(client, email=f"overdue-{uuid.uuid4().hex[:8]}@example.com")


async def test_claim_does_not_starve_behind_expired_tasks(session, client, user):
    """Живое задание выдаётся, даже если перед ним висят просроченные.

    Второй дефект, найденный тем же chaos-тестом: окно кандидатов
    (`limit * 4`) заполнялось просроченными заданиями, они закрывались в
    цикле, и выдача возвращала пустой список — узел простаивал при живой
    очереди. Теперь просрочка отсекается в SQL.
    """
    from sqlalchemy import update as sa_update

    from app.db.models.task import Task as TaskModel
    from tests.conftest import create_published_agent, register_node_client

    agent = await create_published_agent(client, user)
    fresh = await client.post(
        "/api/v1/tasks",
        headers=user.headers,
        json={"agent_id": agent["id"], "input_payload": {"input": {"text": "живое"}}},
    )
    assert fresh.status_code == 201, fresh.text
    fresh_id = fresh.json()["id"]

    # Шесть просроченных заданий с более высоким приоритетом: при старом
    # запросе они занимали всё окно выдачи.
    for _ in range(6):
        stale = await client.post(
            "/api/v1/tasks",
            headers=user.headers,
            json={"agent_id": agent["id"], "input_payload": {"input": {"text": "старое"}}},
        )
        assert stale.status_code == 201, stale.text
    await session.execute(
        sa_update(TaskModel)
        .where(TaskModel.id != uuid.UUID(fresh_id), TaskModel.status == TaskStatus.queued)
        .values(deadline_at=datetime.now(UTC) - timedelta(minutes=5), priority=10)
    )
    await session.commit()

    _, node_client = await register_node_client(client, user)
    claimed = await node_client.request(
        "POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1}
    )
    assert claimed.status_code == 200, claimed.text
    assignments = claimed.json()["assignments"]
    assert [item["task_id"] for item in assignments] == [fresh_id]
