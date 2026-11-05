"""Сервисные тесты оркестрации: границы, ошибки, идемпотентность (этап 4).

Проверяем то, что трудно выразить через HTTP: внутренние инварианты DAG,
поведение при отсутствии исполнителя, исчерпание попыток, дубли и лимиты.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.schemas.orchestration import PlanAdoptRequest, PlanCreate, PlanStepCreate
from app.services import orchestration as svc
from tests.conftest import create_published_agent, register_node_client, version_payload

PY_CODE = "def run(payload):\n    return {'ok': True}\n"


async def _agent(client, user, slug: str, capabilities: list[str]) -> dict:
    agent = await create_published_agent(client, user, name=slug, slug=slug)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/capabilities",
        json={"capabilities": [{"capability_key": key, "declared_level": 3} for key in capabilities]},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _python_agent(client, user, slug: str, capabilities: list[str]) -> dict:
    agent = await create_published_agent(client, user, name=slug, slug=slug)
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="python",
            entrypoint_config={"code": PY_CODE},
        ),
        headers=user.headers,
    )
    assert version.status_code == 201, version.text
    await client.put(
        f"/api/v1/agents/{agent['id']}/capabilities",
        json={"capabilities": [{"capability_key": key, "declared_level": 3} for key in capabilities]},
        headers=user.headers,
    )
    return (await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)).json()


def _step(key: str, caps: list[str] | None = None, deps: list[str] | None = None) -> PlanStepCreate:
    return PlanStepCreate(
        key=key,
        name=f"Шаг {key}",
        required_capabilities=caps or [],
        depends_on=deps or [],
    )


# ── Границы плана ─────────────────────────────────────────────────────
async def test_too_many_steps_rejected(client, user):
    steps = [_step(f"s{i}", ["research.web_search"]) for i in range(21)]
    with pytest.raises(Exception) as exc:
        PlanCreate(title="Слишком много", steps=steps)
    assert "20" in str(exc.value) or "steps" in str(exc.value)


async def test_duplicate_step_keys_rejected_at_schema_level():
    payload = {
        "title": "Дубли",
        "steps": [
            {"key": "a", "name": "Первый", "required_capabilities": [], "depends_on": []},
            {"key": "a", "name": "Второй", "required_capabilities": [], "depends_on": []},
        ],
    }
    with pytest.raises(Exception) as exc:
        PlanCreate.model_validate(payload)
    assert "уникальны" in str(exc.value)


async def test_self_dependency_rejected_at_schema_level():
    with pytest.raises(Exception) as exc:
        PlanCreate.model_validate(
            {
                "title": "Сам на себя",
                "steps": [{"key": "a", "name": "Шаг", "required_capabilities": [], "depends_on": ["a"]}],
            }
        )
    assert "зависеть от себя" in str(exc.value)


async def test_steps_and_planner_are_mutually_exclusive():
    with pytest.raises(Exception) as exc:
        PlanCreate.model_validate(
            {
                "title": "Оба",
                "steps": [{"key": "a", "name": "Шаг", "required_capabilities": [], "depends_on": []}],
                "planner_agent_id": str(uuid.uuid4()),
            }
        )
    assert "не оба" in str(exc.value)


async def test_plan_without_steps_or_planner_rejected():
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        PlanCreate.model_validate({"title": "Пусто", "steps": []})


# ── Внутренние инварианты сервиса ─────────────────────────────────────
async def test_get_plan_raises_not_found(session):
    with pytest.raises(NotFoundError) as exc:
        await svc.get_plan(session, uuid.uuid4())
    assert exc.value.code == "plan_not_found"


async def test_ensure_plan_access_rules(session, user, other_user):
    plan = svc.TaskPlan(
        requester_id=uuid.UUID(user.id),
        title="Права",
        goal={},
    )
    svc.ensure_plan_access(plan, uuid.UUID(user.id))
    svc.ensure_plan_access(plan, uuid.UUID(other_user.id), is_admin=True)
    with pytest.raises(PermissionDeniedError) as exc:
        svc.ensure_plan_access(plan, uuid.UUID(other_user.id))
    assert exc.value.code == "plan_forbidden"


async def test_dag_validation_reports_cycle():
    with pytest.raises(ValidationError) as exc:
        svc._validate_dag([_step("a", deps=["b"]), _step("b", deps=["a"])])
    assert exc.value.code == "cyclic_dependencies"


async def test_dag_validation_reports_unknown_dependency():
    with pytest.raises(ValidationError) as exc:
        svc._validate_dag([_step("a", deps=["ghost"])])
    assert exc.value.code == "unknown_dependency"
    assert "ghost" in str(exc.value)


async def test_capability_key_format_validation():
    with pytest.raises(ValidationError) as exc:
        await svc._validate_capability_keys(["nodots", "research.web_search"])
    assert exc.value.code == "invalid_capability_key"
    assert exc.value.details["invalid"] == ["nodots"]
    # валидные ключи (в том числе новые для таксономии) проходят
    await svc._validate_capability_keys(["research.web_search", "new.category.action"])


async def test_merge_inputs_includes_goal_and_dependency_outputs():
    plan = svc.TaskPlan(title="Цель", goal={"topic": "x"}, requester_id=uuid.uuid4())
    dep = svc.PlanStep(key="dep", name="Зависимость", output_payload={"value": 1},
                       input_payload={}, depends_on=[])
    main = svc.PlanStep(key="main", name="Основной", depends_on=["dep"],
                        input_payload={"own": True})
    merged = svc._merge_inputs(main, plan, [dep, main])
    assert merged["plan"]["goal"] == {"topic": "x"}
    assert merged["input"] == {"own": True}
    assert merged["depends_on_outputs"]["dep"]["value"] == 1


async def test_merge_inputs_skips_dependencies_without_output():
    plan = svc.TaskPlan(title="Цель", goal={}, requester_id=uuid.uuid4())
    dep = svc.PlanStep(key="dep", name="Зависимость", output_payload={}, input_payload={},
                       depends_on=[])
    main = svc.PlanStep(key="main", name="Основной", depends_on=["dep"], input_payload={})
    assert "depends_on_outputs" not in svc._merge_inputs(main, plan, [dep, main])


# ── Поведение при отказах ─────────────────────────────────────────────
async def test_retry_rejects_non_failed_step(client, session, user):
    await _agent(client, user, "svc-a", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Повтор", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    with pytest.raises(ValidationError) as exc:
        await svc.retry_step(session, plan, "one")
    assert exc.value.code == "step_not_retryable"


async def test_retry_unknown_step(client, session, user):
    await _agent(client, user, "svc-b", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Нет шага", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    with pytest.raises(NotFoundError) as exc:
        await svc.retry_step(session, plan, "ghost")
    assert exc.value.code == "step_not_found"


async def test_retry_respects_attempts_limit(client, session, user):
    await _python_agent(client, user, "svc-c", ["research.web_search"])
    _, node_client = await register_node_client(client, user)
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(
            title="Попытки",
            steps=[
                PlanStepCreate(
                    key="one",
                    name="Шаг один",
                    required_capabilities=["research.web_search"],
                    max_attempts=1,
                )
            ],
        ),
    )
    await svc.assign_steps(session, plan)
    await session.commit()
    await svc.run_plan(session, plan)
    await session.commit()

    claim = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 5})
    assignment = claim.json()["assignments"][0]
    await node_client.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{assignment['task_id']}/result",
        {"lease_token": assignment["lease_token"], "success": False, "error_message": "упал"},
    )
    await svc.sync_plan(session, plan)
    await session.commit()

    # Дошли до терминального failed, но попытки шага исчерпаны
    with pytest.raises(ConflictError) as exc:
        await svc.retry_step(session, plan, "one")
    assert exc.value.code == "step_attempts_exhausted"


async def test_cancel_finished_plan_conflict(client, session, user):
    await _agent(client, user, "svc-d", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Отмена", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    await svc.cancel_plan(session, plan, reason="тест")
    await session.commit()
    with pytest.raises(ConflictError) as exc:
        await svc.cancel_plan(session, plan, reason="ещё раз")
    assert exc.value.code == "plan_finished"


async def test_run_finished_plan_conflict(client, session, user):
    await _agent(client, user, "svc-e", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Финал", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    await svc.cancel_plan(session, plan, reason="тест")
    await session.commit()
    with pytest.raises(ConflictError):
        await svc.run_plan(session, plan)


async def test_adopt_twice_conflicts(client, session, user):
    planner = await _agent(client, user, "svc-f", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Декомпозиция", planner_agent_id=uuid.UUID(planner["id"])),
    )
    await session.commit()
    steps = PlanAdoptRequest(steps=[_step("a", ["research.web_search"])])
    await svc.adopt_planner_output(session, plan, steps, actor_id=uuid.UUID(user.id))
    await session.commit()
    # После принятия шагов план уже не черновик, а шаги уже существуют;
    # код `plan_has_steps` приоритетнее — он точнее объясняет причину.
    with pytest.raises(ConflictError) as exc:
        await svc.adopt_planner_output(session, plan, steps, actor_id=uuid.UUID(user.id))
    assert exc.value.code in {"plan_has_steps", "plan_not_draft"}


async def test_blocked_step_is_not_launched(client, session, user):
    """Шаг без возможности-исполнителя не должен создавать задачу."""
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Блок", steps=[_step("x", ["nobody.can_do_this"])]),
    )
    await session.commit()
    assigned, blocked, details = await svc.assign_steps(session, plan)
    assert (assigned, blocked) == (0, 1)
    assert details[0]["applied"] is False
    await session.commit()

    steps = await svc.plan_steps(session, plan.id)
    assert steps[0].status is svc.StepStatus.blocked
    assert steps[0].task_id is None


async def test_plan_without_capabilities_is_blocked(client, session, user):
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Без возможностей", steps=[_step("plain")]),
    )
    await session.commit()
    _, blocked, details = await svc.assign_steps(session, plan)
    assert blocked == 1
    assert "возможности" in details[0]["reason"]


async def test_plan_stats_counts_plans_and_steps(client, session, user):
    await _agent(client, user, "svc-g", ["research.web_search"])
    await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Статистика", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    stats = await svc.plan_stats(session, uuid.UUID(user.id))
    assert stats["plans_total"] == 1
    assert stats["steps"]["ready"] == 1


async def test_list_plans_scoped_by_requester(client, session, user, other_user):
    await _agent(client, user, "svc-h", ["research.web_search"])
    await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="Мой", steps=[_step("one", ["research.web_search"])]),
    )
    await session.commit()
    mine, total_mine = await svc.list_plans(session, requester_id=uuid.UUID(user.id))
    theirs, total_theirs = await svc.list_plans(session, requester_id=uuid.UUID(other_user.id))
    assert total_mine == 1 and len(mine) == 1
    assert total_theirs == 0 and theirs == []


async def test_plan_events_have_monotonic_sequence(client, session, user):
    await _agent(client, user, "svc-i", ["research.web_search"])
    plan = await svc.create_plan(
        session,
        requester_id=uuid.UUID(user.id),
        data=PlanCreate(title="События", steps=[_step("one", ["research.web_search"])]),
    )
    await svc.assign_steps(session, plan)
    await svc.run_plan(session, plan)
    await session.commit()
    events = await svc.plan_events(session, plan.id)
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert events[0].event_type == "plan.created"
