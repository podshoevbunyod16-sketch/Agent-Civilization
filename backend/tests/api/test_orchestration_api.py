"""API-тесты оркестрации: DAG, подбор исполнителей, запуск, retry, права (этап 4)."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import (
    create_published_agent,
    register_node_client,
    version_payload,
)

PY_CODE = "def run(payload):\n    return {'ok': True, 'step': payload.get('step')}\n"


async def _published(client, user, slug: str, capabilities: list[str]) -> dict:
    agent = await create_published_agent(client, user, name=slug, slug=slug)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/capabilities",
        json={
            "capabilities": [
                {"capability_key": key, "declared_level": 3} for key in capabilities
            ]
        },
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _python_agent(client, user, slug: str, capabilities: list[str]) -> dict:
    """Опубликованный python-агент, который реально исполняется узлом."""
    agent = await create_published_agent(client, user, name=slug, slug=slug)
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="python",
            entrypoint_config={"code": PY_CODE},
            resource_limits={"max_wall_seconds": 10, "max_memory_mb": 128},
        ),
        headers=user.headers,
    )
    assert version.status_code == 201, version.text
    detail = await client.put(
        f"/api/v1/agents/{agent['id']}/capabilities",
        json={
            "capabilities": [
                {"capability_key": key, "declared_level": 3} for key in capabilities
            ]
        },
        headers=user.headers,
    )
    assert detail.status_code == 200, detail.text
    return (await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)).json()


def _delta_plan(**overrides) -> dict:
    """План: analyze → (summarize | verify) → publish."""
    payload = {
        "title": "Исследование рынка",
        "goal": {"topic": "агентные платформы"},
        "steps": [
            {
                "key": "analyze",
                "name": "Собрать данные",
                "required_capabilities": ["research.web_search"],
            },
            {
                "key": "summarize",
                "name": "Сделать выжимку",
                "required_capabilities": ["text.summarize"],
                "depends_on": ["analyze"],
            },
            {
                "key": "verify",
                "name": "Проверить факты",
                "required_capabilities": ["research.fact_check"],
                "depends_on": ["analyze"],
            },
            {
                "key": "publish",
                "name": "Опубликовать отчёт",
                "required_capabilities": ["text.summarize"],
                "depends_on": ["summarize", "verify"],
            },
        ],
        "auto_start": False,
    }
    payload.update(overrides)
    return payload


async def _claim_and_report(client, node_client, *, success=True):
    response = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 5})
    assert response.status_code == 200, response.text
    assignments = response.json()["assignments"]
    for assignment in assignments:
        body = {
            "lease_token": assignment["lease_token"],
            "success": success,
            "output": {"ok": True, "step": assignment["input_payload"]["step"]["key"]},
            "error_message": None if success else "агент упал",
        }
        reported = await node_client.request(
            "POST",
            f"/api/v1/nodes/me/tasks/{assignment['task_id']}/result",
            body,
        )
        assert reported.status_code == 200, reported.text
    return assignments


# ── Создание и валидация ──────────────────────────────────────────────
async def test_create_plan_builds_dependencies_and_readiness(client, user):
    await _published(client, user, "orch-a", ["research.web_search"])
    await _published(client, user, "orch-b", ["text.summarize"])
    await _published(client, user, "orch-c", ["research.fact_check"])

    response = await client.post("/api/v1/plans", json=_delta_plan(), headers=user.headers)
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["status"] == "planned"
    steps = {step["key"]: step for step in plan["steps"]}
    assert steps["analyze"]["status"] == "ready"
    assert steps["summarize"]["status"] == "pending"
    assert steps["verify"]["status"] == "pending"
    assert steps["publish"]["status"] == "pending"
    assert steps["analyze"]["agent_id"] is not None
    assert steps["analyze"]["match_score"] is not None
    assert any(event["event_type"] == "plan.created" for event in plan["events"])


async def test_plan_rejects_cyclic_dependencies(client, user):
    await _published(client, user, "cyc-a", ["research.web_search"])
    payload = _delta_plan(
        steps=[
            {"key": "a", "name": "Шаг А", "required_capabilities": ["research.web_search"],
             "depends_on": ["b"]},
            {"key": "b", "name": "Шаг Б", "required_capabilities": ["research.web_search"],
             "depends_on": ["a"]},
        ]
    )
    response = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cyclic_dependencies"


async def test_plan_rejects_unknown_dependency(client, user):
    await _published(client, user, "unk-a", ["research.web_search"])
    payload = _delta_plan(
        steps=[
            {"key": "a", "name": "Шаг А", "required_capabilities": ["research.web_search"],
             "depends_on": ["ghost"]},
        ]
    )
    response = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_dependency"


async def test_plan_rejects_malformed_capability_key(client, user):
    payload = _delta_plan(
        steps=[{"key": "bad", "name": "Плохой шаг", "required_capabilities": ["NoDotsHere"]}]
    )
    response = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_capability_key"


async def test_plan_requires_steps_or_planner(client, user):
    response = await client.post(
        "/api/v1/plans", json={"title": "Пусто", "steps": []}, headers=user.headers
    )
    assert response.status_code == 422


async def test_plan_blocks_step_without_suitable_agent(client, user):
    await _published(client, user, "solo-a", ["research.web_search"])
    blocked_payload = _delta_plan(
        steps=[
            {
                "key": "impossible",
                "name": "Невозможный шаг",
                "required_capabilities": ["research.fact_check"],
            },
        ]
    )
    blocked = await client.post(
        "/api/v1/plans", json=blocked_payload, headers=user.headers
    )
    assert blocked.status_code == 201, blocked.text
    assert blocked.json()["steps"][0]["status"] == "blocked"
    assert blocked.json()["steps"][0]["note"]


async def test_assign_endpoint_returns_match_details(client, user):
    await _published(client, user, "assign-a", ["research.web_search"])
    payload = _delta_plan(
        steps=[
            {"key": "one", "name": "Шаг один", "required_capabilities": ["research.web_search"]},
        ]
    )
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/plans/{plan_id}/assign", headers=user.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assigned"] == 1
    assert body["steps"][0]["step_key"] == "one"
    assert body["steps"][0]["agent_slug"] == "assign-a"
    assert body["steps"][0]["score"] > 0


# ── Полный прогон через реальный узел ─────────────────────────────────
async def test_plan_runs_end_to_end_through_node(client, user):
    await _python_agent(client, user, "flow-a", ["research.web_search"])
    await _python_agent(client, user, "flow-b", ["text.summarize"])
    await _python_agent(client, user, "flow-c", ["research.fact_check"])
    _, node_client = await register_node_client(client, user)

    payload = _delta_plan(auto_start=False)
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]

    # 1. старт: должны запуститься только шаги без зависимостей (analyze)
    run1 = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert run1.status_code == 200, run1.text
    assert run1.json()["launched"] == ["analyze"]
    assert run1.json()["status"] == "running"

    assignments = await _claim_and_report(client, node_client)
    assert len(assignments) == 1

    # 2. синхронизация: analyze готов, summarize/verify стали ready
    progress = await client.get(f"/api/v1/plans/{plan_id}/progress", headers=user.headers)
    assert progress.json()["by_status"]["succeeded"] == 1

    run2 = await client.post(
        f"/api/v1/plans/{plan_id}/run?limit=10", headers=user.headers
    )
    assert run2.status_code == 200, run2.text
    assert sorted(run2.json()["launched"]) == ["summarize", "verify"]

    await _claim_and_report(client, node_client)

    run3 = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert run3.json()["launched"] == ["publish"]
    await _claim_and_report(client, node_client)

    # 3. финальный прогон: план завершён, все шаги успешны
    final = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert final.status_code == 200
    body = final.json()
    assert body["status"] == "succeeded"
    assert sorted(body["completed"]) == ["analyze", "publish", "summarize", "verify"]
    assert body["progress"]["percent"] == 100.0

    detail = await client.get(f"/api/v1/plans/{plan_id}", headers=user.headers)
    steps = {step["key"]: step for step in detail.json()["steps"]}
    assert steps["publish"]["output_payload"]
    assert steps["publish"]["duration_ms"] is not None
    assert steps["summarize"]["task_id"] is not None
    # Шаг получил на вход результат зависимости (снимок входа сохраняется),
    # а собственный результат шага остаётся его собственным
    assert steps["summarize"]["resolved_input"]["depends_on_outputs"]["analyze"]["ok"] is True
    assert steps["summarize"]["output_payload"]["step"] == "summarize"
    assert steps["publish"]["resolved_input"]["depends_on_outputs"]["verify"]["ok"] is True
    event_types = [event["event_type"] for event in detail.json()["events"]]
    assert "plan.started" in event_types
    assert "plan.succeeded" in event_types
    assert "step.launched" in event_types


async def test_plan_respects_max_parallelism(client, user):
    await _python_agent(client, user, "par-a", ["research.web_search"])
    await _python_agent(client, user, "par-b", ["text.summarize"])
    await register_node_client(client, user)
    payload = _delta_plan(
        max_parallelism=1,
        steps=[
            {"key": "x", "name": "Шаг X", "required_capabilities": ["research.web_search"]},
            {"key": "y", "name": "Шаг Y", "required_capabilities": ["text.summarize"]},
            {"key": "z", "name": "Шаг Z", "required_capabilities": ["research.fact_check"]},
        ],
    )
    # fact_check никому не заявлен — шаг заблокируется, остальные пойдут
    await _python_agent(client, user, "par-c", ["research.fact_check"])
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]

    first = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert len(first.json()["launched"]) == 1, first.text


async def test_failed_step_blocks_dependents_and_fails_plan(client, user):
    await _python_agent(client, user, "fail-a", ["research.web_search"])
    await _python_agent(client, user, "fail-b", ["text.summarize"])
    _, node_client = await register_node_client(client, user)

    payload = _delta_plan(
        steps=[
            {"key": "first", "name": "Первый", "required_capabilities": ["research.web_search"]},
            {"key": "second", "name": "Второй", "required_capabilities": ["text.summarize"],
             "depends_on": ["first"]},
        ],
        failure_policy="fail_plan",
    )
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]

    await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    # Первый отказ уходит в повтор на уровне задачи (аренда/retry runtime),
    # пока не исчерпаны max_attempts — план ещё выполняется.
    await _claim_and_report(client, node_client, success=False)
    retry_run = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert retry_run.status_code == 200
    assert retry_run.json()["status"] == "running"

    await _claim_and_report(client, node_client, success=False)

    result = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "failed"
    assert "first" in body["failed"]

    detail = await client.get(f"/api/v1/plans/{plan_id}", headers=user.headers)
    steps = {step["key"]: step for step in detail.json()["steps"]}
    assert steps["first"]["status"] == "failed"
    assert steps["first"]["error"] is not None
    assert steps["second"]["status"] in {"skipped", "pending"}


async def test_retry_step_after_failure(client, user):
    await _python_agent(client, user, "retry-a", ["research.web_search"])
    _, node_client = await register_node_client(client, user)
    payload = _delta_plan(
        steps=[
            {"key": "only", "name": "Единственный", "required_capabilities":
                ["research.web_search"], "max_attempts": 2},
        ]
    )
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]
    await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    await _claim_and_report(client, node_client, success=False)
    # runtime даёт задаче повтор: исчерпываем попытки, чтобы шаг стал failed
    await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    await _claim_and_report(client, node_client, success=False)
    await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)

    retry = await client.post(
        f"/api/v1/plans/{plan_id}/steps/only/retry", headers=user.headers
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["step"]["status"] in {"ready", "pending"}
    assert retry.json()["status"] == "running"

    await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    await _claim_and_report(client, node_client)
    final = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert final.json()["status"] == "succeeded"


async def test_cancel_plan_cancels_tasks(client, user):
    await _python_agent(client, user, "can-a", ["research.web_search"])
    await register_node_client(client, user)
    payload = _delta_plan(
        steps=[
            {"key": "work", "name": "Работа", "required_capabilities": ["research.web_search"]},
        ]
    )
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]
    run = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert run.json()["launched"] == ["work"]

    cancel = await client.post(
        f"/api/v1/plans/{plan_id}/cancel?reason=тест", headers=user.headers
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"

    detail = await client.get(f"/api/v1/plans/{plan_id}", headers=user.headers)
    assert detail.json()["steps"][0]["status"] == "skipped"

    again = await client.post(f"/api/v1/plans/{plan_id}/run", headers=user.headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "plan_finished"


# ── Делегированная декомпозиция (агент-планировщик) ───────────────────
async def test_planner_agent_decomposition_flow(client, user):
    planner = await _published(client, user, "planner-a", ["research.web_search"])
    await _published(client, user, "worker-b", ["research.web_search"])

    created = await client.post(
        "/api/v1/plans",
        json={
            "title": "Делегированная декомпозиция",
            "goal": {"task": "разбей на шаги"},
            "planner_agent_id": planner["id"],
            "auto_start": False,
        },
        headers=user.headers,
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "draft"
    assert plan["planner_agent_id"] == planner["id"]
    assert plan["planner_message_id"] is not None
    assert plan["steps"] == []
    assert any(
        event["event_type"] == "plan.decomposition_requested" for event in plan["events"]
    )

    adopted = await client.post(
        f"/api/v1/plans/{plan['id']}/adopt",
        json={
            "reason": "план от агента",
            "steps": [
                {"key": "collect", "name": "Собрать", "required_capabilities":
                    ["research.web_search"]},
                {"key": "digest", "name": "Сжать", "required_capabilities":
                    ["research.web_search"], "depends_on": ["collect"]},
            ],
        },
        headers=user.headers,
    )
    assert adopted.status_code == 200, adopted.text
    body = adopted.json()
    assert len(body["steps"]) == 2
    assert body["status"] == "planned"
    assert all(step["agent_id"] for step in body["steps"])


async def test_adopt_rejects_invalid_dag(client, user):
    planner = await _published(client, user, "planner-b", ["research.web_search"])
    created = await client.post(
        "/api/v1/plans",
        json={"title": "Плохая декомпозиция", "planner_agent_id": planner["id"]},
        headers=user.headers,
    )
    plan_id = created.json()["id"]
    response = await client.post(
        f"/api/v1/plans/{plan_id}/adopt",
        json={
            "steps": [
                {"key": "a", "name": "Шаг А", "required_capabilities": [], "depends_on": ["b"]},
                {"key": "b", "name": "Шаг Б", "required_capabilities": [], "depends_on": ["a"]},
            ]
        },
        headers=user.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cyclic_dependencies"


async def test_parse_planner_output_contract():
    from app.core.errors import ValidationError
    from app.services.orchestration import parse_planner_output

    parsed = parse_planner_output(
        {"steps": [{"key": "a", "name": "Первый", "required_capabilities": [], "input": {}}]}
    )
    assert parsed.steps[0].key == "a"

    with pytest.raises(ValidationError) as exc:
        parse_planner_output({"steps": []})
    assert exc.value.code == "invalid_planner_output"


# ── Права и видимость ─────────────────────────────────────────────────
async def test_plans_are_isolated_between_users(client, user, other_user):
    await _published(client, user, "iso-a", ["research.web_search"])
    payload = _delta_plan(
        steps=[
            {"key": "one", "name": "Шаг", "required_capabilities": ["research.web_search"]},
        ]
    )
    created = await client.post("/api/v1/plans", json=payload, headers=user.headers)
    plan_id = created.json()["id"]

    foreign = await client.get(f"/api/v1/plans/{plan_id}", headers=other_user.headers)
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "plan_forbidden"

    listing = await client.get("/api/v1/plans", headers=other_user.headers)
    assert listing.json()["items"] == []

    run = await client.post(f"/api/v1/plans/{plan_id}/run", headers=other_user.headers)
    assert run.status_code == 403


async def test_anonymous_cannot_access_plans(client):
    response = await client.get(f"/api/v1/plans/{uuid.uuid4()}")
    assert response.status_code == 401
    create = await client.post("/api/v1/plans", json={"title": "x"})
    assert create.status_code == 401


async def test_plan_stats(client, user):
    await _published(client, user, "stat-a", ["research.web_search"])
    payload = _delta_plan(
        steps=[
            {"key": "one", "name": "Шаг", "required_capabilities": ["research.web_search"]},
        ]
    )
    await client.post("/api/v1/plans", json=payload, headers=user.headers)
    response = await client.get("/api/v1/plans/stats", headers=user.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["plans_total"] == 1
    assert body["steps"]["ready"] == 1
