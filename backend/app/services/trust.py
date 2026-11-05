"""Верификация результатов, репутация и доверие (этап 5).

Принципы, которые нельзя нарушать:

1. **Репутация выводима.** `agents.reputation_score` — кэш суммы
   `reputation_events` (с учётом отзывов через апелляции). Любое число можно
   объяснить: откуда, когда и за что.
2. **Заявленное ≠ проверенное.** `declared_level` заполняет разработчик,
   `verified_level` — только этот сервис, и только на основании пройденных
   проверок результатов.
3. **Проверка не выполняется в процессе API.** Тест-код и проверки
   исполняются в песочнице (`SandboxRunner`); backend никогда не запускает
   недоверенный код сам.
4. **Независимость проверки.** Верификатор не может быть тем же агентом
   или владельцем исполнителя; независимые проверки весят больше.

Модель репутации (детерминированная, объяснимая):

    score = 100 * (positive + PRIOR) / (positive + negative + 2 * PRIOR)

где `PRIOR = 2.5` — симметричное сглаживание: новый агент стартует с 50,
а не с 0 или 100, и ни один единственный отзыв не обнуляет историю. Вес события зависит от
источника: успешное задание — 1, независимая проверка — 2, отзыв 5★ — 1.5,
аномалия — до 5. Домен — [0, 100].
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import Agent, AgentCapability, AgentStatus
from app.db.models.task import Task, TaskStatus
from app.db.models.trust import (
    AnomalySignal,
    AnomalyStatus,
    AnomalyType,
    Appeal,
    AppealStatus,
    ReputationEvent,
    ReputationSource,
    TaskReview,
    TaskVerdict,
    TaskVerification,
    Verdict,
    VerificationMethod,
    VerificationStatus,
)
from app.db.models.user import User
from app.sandbox import SandboxRunner, SandboxUnavailableError

logger = logging.getLogger(__name__)

# Симметричное сглаживание: новый агент = 50, не 0 и не 100
PRIOR = 2.5
# Вес вклада по источнику: чем независимее оценка, тем больше цена
SOURCE_WEIGHTS: dict[ReputationSource, float] = {
    ReputationSource.task_success: 1.0,
    ReputationSource.task_failure: 1.0,
    ReputationSource.verification_passed: 2.0,
    ReputationSource.verification_failed: 2.5,
    ReputationSource.review: 1.5,
    ReputationSource.anomaly_penalty: 5.0,
    ReputationSource.appeal: 2.0,
    ReputationSource.manual: 1.0,
}
# Доля пройденных проверок, ниже которой возможность считается неподтверждённой
CAPABILITY_VERIFY_THRESHOLD = 0.8
# Сколько успешных проверок нужно для подтверждения уровня
CAPABILITY_MIN_CHECKS = 2


def _now() -> datetime:
    return datetime.now(UTC)


# ── Репутация ──────────────────────────────────────────────────────────
def compute_reputation(positive: float, negative: float) -> float:
    """Детерминированная формула репутации (см. docstring модуля)."""
    positive = max(0.0, positive)
    negative = abs(negative)
    score = 100.0 * (positive + PRIOR) / (positive + negative + 2 * PRIOR)
    return round(min(100.0, max(0.0, score)), 2)


async def _recompute_agent_reputation(
    session: AsyncSession, agent_id: uuid.UUID
) -> tuple[float, float, float]:
    """Пересчитывает кэш репутации из событий. Возвращает (score, positive, negative)."""
    rows = (
        await session.execute(
            select(ReputationEvent.delta, ReputationEvent.weight).where(
                ReputationEvent.agent_id == agent_id,
                ReputationEvent.reversed_at.is_(None),
            )
        )
    ).all()
    positive = sum(abs(delta) * weight for delta, weight in rows if delta > 0)
    negative = sum(abs(delta) * weight for delta, weight in rows if delta < 0)
    score = compute_reputation(positive, negative)
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is not None:
        agent.reputation_score = score
    return score, positive, negative


async def record_reputation(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    source: ReputationSource,
    delta: float,
    reason: str = "",
    dedup_key: str | None = None,
    task_id: uuid.UUID | None = None,
    verification_id: uuid.UUID | None = None,
    review_id: uuid.UUID | None = None,
    anomaly_id: uuid.UUID | None = None,
    weight: float | None = None,
) -> ReputationEvent | None:
    """Записывает вклад в репутацию. Идемпотентно по `dedup_key`.

    Возвращает None, если такой вклад уже был (повторная проверка, повторный
    отчёт узла) — это защищает от «накрутки» репутации повторами.
    """
    if dedup_key:
        existing = (
            await session.execute(
                select(ReputationEvent).where(
                    ReputationEvent.agent_id == agent_id,
                    ReputationEvent.dedup_key == dedup_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

    event = ReputationEvent(
        agent_id=agent_id,
        source=source,
        delta=delta,
        weight=weight if weight is not None else SOURCE_WEIGHTS.get(source, 1.0),
        reason=reason[:280],
        task_id=task_id,
        verification_id=verification_id,
        review_id=review_id,
        anomaly_id=anomaly_id,
        dedup_key=dedup_key,
    )
    session.add(event)
    await session.flush()
    await _recompute_agent_reputation(session, agent_id)
    return event


async def reputation_breakdown(
    session: AsyncSession, agent_id: uuid.UUID
) -> dict[str, Any]:
    """Объяснение рейтинга: по источникам и последние события."""
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")

    rows = (
        await session.execute(
            select(
                ReputationEvent.source,
                func.count(),
                func.sum(ReputationEvent.delta * ReputationEvent.weight),
            )
            .where(ReputationEvent.agent_id == agent_id, ReputationEvent.reversed_at.is_(None))
            .group_by(ReputationEvent.source)
        )
    ).all()
    by_source = {
        source.value: {"count": int(count), "delta": round(float(total or 0), 3)}
        for source, count, total in rows
    }
    recent = (
        await session.execute(
            select(ReputationEvent)
            .where(ReputationEvent.agent_id == agent_id)
            .order_by(ReputationEvent.created_at.desc())
            .limit(10)
        )
    ).scalars().all()

    positive = sum(item["delta"] for item in by_source.values() if item["delta"] > 0)
    negative = sum(item["delta"] for item in by_source.values() if item["delta"] < 0)
    return {
        "agent_id": agent_id,
        "reputation_score": float(agent.reputation_score),
        "formula_score": compute_reputation(positive, negative),
        "positive": round(positive, 3),
        "negative": round(abs(negative), 3),
        "by_source": by_source,
        "events": [
            {
                "id": event.id,
                "source": event.source.value,
                "delta": event.delta,
                "weight": event.weight,
                "reason": event.reason,
                "task_id": event.task_id,
                "reversed_at": event.reversed_at,
                "created_at": event.created_at,
            }
            for event in recent
        ],
        "verified_results": int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(TaskVerdict)
                    .where(
                        TaskVerdict.agent_id == agent_id,
                        TaskVerdict.verdict == Verdict.verified,
                    )
                )
            ).scalar_one()
        ),
    }


# ── Обновление счётчиков задачи (хук из gateway) ───────────────────────
async def on_task_finished(
    session: AsyncSession, task: Task, *, success: bool
) -> None:
    """Фиксирует вклад задания в репутацию (идемпотентно по task_id)."""
    source = (
        ReputationSource.task_success if success else ReputationSource.task_failure
    )
    await record_reputation(
        session,
        agent_id=task.agent_id,
        source=source,
        delta=1.0 if success else -1.0,
        reason=f"Задание {task.id} {'выполнено' if success else 'провалено'}",
        dedup_key=f"task:{task.id}:{'success' if success else 'failure'}",
        task_id=task.id,
    )


# ── Проверки результата ────────────────────────────────────────────────
def _json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def validate_against_schema(
    payload: Any, schema: dict[str, Any], *, path: str = "$"
) -> list[str]:
    """Минимальная проверка JSON-схемы без внешних зависимостей.

    Поддерживается подмножество, достаточное для контрактов агентов:
    `type`, `required`, `properties`, `items`, `minimum`, `maximum`,
    `minLength`, `maxLength`, `enum`. Неизвестные ключи игнорируются —
    цель проверки не «полный валидатор», а объективный контроль формы.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: схема должна быть объектом"]

    expected = schema.get("type")
    if expected:
        actual = _json_type_name(payload)
        allowed = expected if isinstance(expected, list) else [expected]
        type_ok = actual in allowed or (actual == "number" and "integer" in allowed)
        if "integer" in allowed and actual == "number" and isinstance(payload, float):
            type_ok = payload.is_integer()
        if not type_ok:
            errors.append(f"{path}: ожидался {expected}, получен {actual}")
            return errors

    if isinstance(payload, dict):
        for key in schema.get("required", []) or []:
            if key not in payload:
                errors.append(f"{path}.{key}: обязательное поле отсутствует")
        properties = schema.get("properties") or {}
        for key, subschema in properties.items():
            if key in payload:
                errors.extend(
                    validate_against_schema(payload[key], subschema, path=f"{path}.{key}")
                )
    if isinstance(payload, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(payload[:50]):
            errors.extend(
                validate_against_schema(item, schema["items"], path=f"{path}[{index}]")
            )
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if "minimum" in schema and payload < schema["minimum"]:
            errors.append(f"{path}: {payload} < minimum {schema['minimum']}")
        if "maximum" in schema and payload > schema["maximum"]:
            errors.append(f"{path}: {payload} > maximum {schema['maximum']}")
    if isinstance(payload, str):
        if "minLength" in schema and len(payload) < schema["minLength"]:
            errors.append(f"{path}: длина {len(payload)} < {schema['minLength']}")
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            errors.append(f"{path}: длина {len(payload)} > {schema['maxLength']}")
    if "enum" in schema and payload not in schema["enum"]:
        errors.append(f"{path}: значение не из enum")
    return errors


_PATH_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "len_gte", "len_lte", "exists"}


def _resolve_path(payload: Any, path: str) -> tuple[bool, Any]:
    current = payload
    for part in path.split("."):
        # Доступ только по ключам словаря: никаких атрибутов, dunder запрещены
        if part.startswith("__"):
            return False, None
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def evaluate_assertions(payload: Any, assertions: list[dict[str, Any]]) -> dict[str, Any]:
    """Декларативные проверки результата: `{"path": "a.b", "op": "eq", "value": 1}`."""
    results: list[dict[str, Any]] = []
    for assertion in assertions:
        path = str(assertion.get("path", ""))
        op = str(assertion.get("op", "exists"))
        if not _PATH_RE.match(path):
            results.append({"path": path, "ok": False, "error": "некорректный путь"})
            continue
        if op not in _OPERATORS:
            results.append({"path": path, "ok": False, "error": f"неизвестный оператор {op}"})
            continue
        found, actual = _resolve_path(payload, path)
        expected = assertion.get("value")
        ok = False
        if op == "exists":
            ok = found
        elif not found:
            ok = False
        elif op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "gt":
            ok = isinstance(actual, (int, float)) and actual > expected
        elif op == "gte":
            ok = isinstance(actual, (int, float)) and actual >= expected
        elif op == "lt":
            ok = isinstance(actual, (int, float)) and actual < expected
        elif op == "lte":
            ok = isinstance(actual, (int, float)) and actual <= expected
        elif op == "contains":
            try:
                ok = expected in actual
            except TypeError:
                ok = False
        elif op == "len_gte":
            try:
                ok = len(actual) >= int(expected)
            except (TypeError, ValueError):
                ok = False
        elif op == "len_lte":
            try:
                ok = len(actual) <= int(expected)
            except (TypeError, ValueError):
                ok = False
        results.append({"path": path, "op": op, "ok": bool(ok), "actual": actual})
    passed = bool(results) and all(item["ok"] for item in results)
    return {"passed": passed, "results": results}


async def _create_verification(
    session: AsyncSession,
    *,
    task: Task,
    method: VerificationMethod,
    dedup_key: str,
    weight: float,
    verifier_id: uuid.UUID | None = None,
    verifier_agent_id: uuid.UUID | None = None,
) -> TaskVerification | None:
    existing = (
        await session.execute(
            select(TaskVerification).where(
                TaskVerification.task_id == task.id,
                TaskVerification.dedup_key == dedup_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    verification = TaskVerification(
        task_id=task.id,
        agent_id=task.agent_id,
        method=method,
        status=VerificationStatus.pending,
        weight=weight,
        verifier_id=verifier_id,
        verifier_agent_id=verifier_agent_id,
        dedup_key=dedup_key,
    )
    session.add(verification)
    await session.flush()
    return verification


async def _finish_verification(
    session: AsyncSession,
    verification: TaskVerification,
    *,
    status: VerificationStatus,
    passed: bool | None,
    details: dict[str, Any],
    score: float | None = None,
    error: dict[str, Any] | None = None,
    started: datetime | None = None,
) -> None:
    verification.status = status
    verification.passed = passed
    verification.score = score
    verification.details = details
    verification.error = error
    verification.finished_at = _now()
    verification.started_at = started or verification.started_at or _now()
    if verification.started_at:
        verification.duration_ms = int(
            (verification.finished_at - verification.started_at).total_seconds() * 1000
        )
    await session.flush()


async def run_automatic_checks(
    session: AsyncSession,
    task: Task,
    config: dict[str, Any],
    *,
    runner: SandboxRunner | None = None,
) -> list[TaskVerification]:
    """Автоматические проверки результата: схема, утверждения, тест-код.

    `config` берётся из версии агента (`verification` в entrypoint_config)
    или передаётся явно при запросе проверки.
    """
    if task.status is not TaskStatus.succeeded:
        raise ValidationError(
            "Проверять можно только успешно завершённое задание",
            code="task_not_verifiable",
            details={"status": task.status.value},
        )
    output = task.output_payload or {}
    created: list[TaskVerification] = []

    schema = config.get("output_schema") or {}
    if schema:
        verification = await _create_verification(
            session, task=task, method=VerificationMethod.schema,
            dedup_key=f"schema:{task.result_hash or task.id}", weight=1.0,
        )
        if verification is not None:
            started = _now()
            errors = validate_against_schema(output, schema)
            await _finish_verification(
                session,
                verification,
                status=VerificationStatus.passed if not errors else VerificationStatus.failed,
                passed=not errors,
                score=1.0 if not errors else 0.0,
                details={"errors": errors[:20], "schema": schema},
                started=started,
            )
            created.append(verification)

    assertions = config.get("assertions") or []
    if assertions:
        verification = await _create_verification(
            session, task=task, method=VerificationMethod.assertions,
            dedup_key=f"assertions:{task.result_hash or task.id}", weight=1.0,
        )
        if verification is not None:
            started = _now()
            outcome = evaluate_assertions(output, assertions)
            await _finish_verification(
                session,
                verification,
                status=(
                    VerificationStatus.passed
                    if outcome["passed"]
                    else VerificationStatus.failed
                ),
                passed=outcome["passed"],
                score=1.0 if outcome["passed"] else 0.0,
                details=outcome,
                started=started,
            )
            created.append(verification)

    test_code = config.get("test_code") or ""
    if str(test_code).strip():
        verification = await _create_verification(
            session, task=task, method=VerificationMethod.auto_test,
            dedup_key=f"auto_test:{task.result_hash or task.id}", weight=1.5,
        )
        if verification is not None:
            started = _now()
            sandbox = runner or SandboxRunner.from_limits(config.get("resource_limits"))
            try:
                result = await sandbox.run_python(
                    str(test_code), {"output": output, "input": task.input_payload}
                )
            except SandboxUnavailableError as exc:
                await _finish_verification(
                    session,
                    verification,
                    status=VerificationStatus.error,
                    passed=None,
                    details={},
                    error={"type": "SandboxUnavailable", "message": str(exc)[:500]},
                    started=started,
                )
            else:
                if result.status == "succeeded":
                    verdict = result.output.get("passed", result.output.get("ok"))
                    await _finish_verification(
                        session,
                        verification,
                        status=VerificationStatus.passed if verdict else VerificationStatus.failed,
                        passed=bool(verdict),
                        score=1.0 if verdict else 0.0,
                        details={"output": result.output, "stdout": result.stdout[-1000:]},
                        started=started,
                    )
                else:
                    await _finish_verification(
                        session,
                        verification,
                        status=VerificationStatus.error
                        if result.status == "timeout"
                        else VerificationStatus.failed,
                        passed=False if result.status != "timeout" else None,
                        details={},
                        error=result.error
                        or {"type": "SandboxError", "message": result.status},
                        started=started,
                    )
            created.append(verification)

    await session.flush()
    await aggregate_verdict(session, task)
    return created


async def request_peer_verification(
    session: AsyncSession,
    task: Task,
    *,
    verifier_agent_id: uuid.UUID,
    requester_id: uuid.UUID,
    config: dict[str, Any] | None = None,
) -> TaskVerification:
    """Независимая проверка другим агентом.

    Агент-верификатор получает результат и должен вернуть `{"verdict": bool}`.
    Проверка выполняется здесь же через его точку входа (sandbox/http) —
    но только если она разрешена: агент опубликован, не совпадает с
    исполнителем, и его владелец — не владелец исполнителя.
    """
    from app.services import agents as agent_service

    executor = await agent_service.get_agent(session, task.agent_id)
    verifier = await agent_service.get_agent(session, verifier_agent_id)
    if verifier.id == executor.id:
        raise ValidationError(
            "Агент не может проверять собственный результат", code="self_verification_forbidden"
        )
    if verifier.owner_id == executor.owner_id:
        # Один владелец у исполнителя и верификатора — независимости нет.
        # Проверяем раньше типа верификатора, иначе причина отказа неверна.
        raise ValidationError(
            "Независимая проверка требует другого владельца агента",
            code="verifier_not_independent",
        )
    if verifier.status is not AgentStatus.published:
        raise ValidationError("Агент-верификатор не опубликован", code="agent_not_published")

    verification = await _create_verification(
        session,
        task=task,
        method=VerificationMethod.peer_agent,
        dedup_key=f"peer:{verifier.id}:{task.result_hash or task.id}",
        weight=2.5,
        verifier_id=requester_id,
        verifier_agent_id=verifier.id,
    )
    if verification is None:
        raise ConflictError("Такая независимая проверка уже запрошена", code="verification_exists")

    started = _now()
    verification.status = VerificationStatus.running
    await session.flush()

    version = next(
        (v for v in verifier.versions if v.version == verifier.current_version), None
    )
    payload = {
        "task": "verify",
        "output": task.output_payload,
        "input": task.input_payload,
        "result_hash": task.result_hash,
        "contract": '{"verdict": bool, "score": 0..1, "comment": str}',
    }
    config = config or {}
    try:
        from app.services import messaging as messaging_service

        strategy = messaging_service._strategy_for(
            verifier, version.entrypoint_type if version else None
        )
        if strategy.value == "sandbox":
            code = str((version.entrypoint_config or {}).get("code", ""))
            if not code.strip():
                raise ValidationError(
                    "У агента-верификатора нет кода", code="verifier_without_code"
                )
            sandbox = SandboxRunner.from_limits(version.resource_limits if version else None)
            result = await sandbox.run_python(code, payload)
            if result.status != "succeeded":
                await _finish_verification(
                    session,
                    verification,
                    status=VerificationStatus.error,
                    passed=None,
                    details={},
                    error=result.error or {"type": "SandboxError", "message": result.status},
                    started=started,
                )
                await aggregate_verdict(session, task)
                return verification
            answer = result.output
        else:
            raise ValidationError(
                "Независимая проверка поддерживает python-агентов; "
                "для внешних агентов используйте HTTP-верификацию задания",
                code="verifier_not_supported",
            )
    except ValidationError:
        raise
    except Exception as exc:  # инфраструктурный сбой проверки
        await _finish_verification(
            session,
            verification,
            status=VerificationStatus.error,
            passed=None,
            details={},
            error={"type": exc.__class__.__name__, "message": str(exc)[:500]},
            started=started,
        )
        await aggregate_verdict(session, task)
        return verification

    verdict_value = answer.get("verdict", answer.get("passed", answer.get("ok")))
    score = answer.get("score")
    await _finish_verification(
        session,
        verification,
        status=VerificationStatus.passed if verdict_value else VerificationStatus.failed,
        passed=bool(verdict_value),
        score=float(score) if isinstance(score, (int, float)) else (1.0 if verdict_value else 0.0),
        details={"answer": answer, "verifier_agent_id": str(verifier.id)},
        started=started,
    )
    await aggregate_verdict(session, task)
    return verification


async def submit_human_verification(
    session: AsyncSession,
    task: Task,
    *,
    actor: User,
    passed: bool,
    comment: str = "",
) -> TaskVerification:
    """Ручная проверка владельцем задания или админом."""
    if task.requester_id != actor.id and not actor.is_admin:
        raise PermissionDeniedError(
            "Проверить результат может владелец задания или админ", code="verification_forbidden"
        )
    verification = await _create_verification(
        session,
        task=task,
        method=VerificationMethod.human,
        dedup_key=f"human:{actor.id}",
        weight=3.0,
        verifier_id=actor.id,
    )
    if verification is None:
        raise ConflictError("Вы уже проверяли это задание", code="verification_exists")
    started = _now()
    await _finish_verification(
        session,
        verification,
        status=VerificationStatus.passed if passed else VerificationStatus.failed,
        passed=passed,
        score=1.0 if passed else 0.0,
        details={"comment": comment[:1000], "actor_id": str(actor.id)},
        started=started,
    )
    await aggregate_verdict(session, task)
    return verification


# ── Агрегация вердикта ─────────────────────────────────────────────────
async def aggregate_verdict(session: AsyncSession, task: Task) -> TaskVerdict:
    """Сводит проверки задачи в один вердикт и влияет на репутацию.

    Правила:
    * все завершённые проверки пройдены (вес ≥ 1) → `verified`;
    * провалена хотя бы одна авторитетная проверка (вес ≥ 1.5) → `rejected`;
    * есть и пройденные, и проваленные → `disputed`;
    * проверок нет или все `error`/`skipped` → `unverified`.
    """
    checks = list(
        (
            await session.execute(
                select(TaskVerification).where(TaskVerification.task_id == task.id)
            )
        ).scalars().all()
    )
    finished = [check for check in checks if check.status.is_finished]
    informative = [
        check
        for check in finished
        if check.status in {VerificationStatus.passed, VerificationStatus.failed}
    ]
    passed_checks = [check for check in informative if check.passed]
    failed_checks = [check for check in informative if check.passed is False]
    independent = [check for check in informative if check.weight >= 2.0]
    total_weight = sum(check.weight for check in informative)
    passed_weight = sum(check.weight for check in passed_checks)

    if not informative:
        verdict = Verdict.unverified
        score = None
    elif not failed_checks:
        verdict = Verdict.verified
        score = round(passed_weight / total_weight, 3) if total_weight else 1.0
    elif not passed_checks:
        verdict = Verdict.rejected
        score = 0.0
    elif any(check.weight >= 1.5 for check in failed_checks):
        verdict = Verdict.rejected
        score = round(passed_weight / total_weight, 3) if total_weight else 0.0
    else:
        verdict = Verdict.disputed
        score = round(passed_weight / total_weight, 3) if total_weight else 0.5

    row = (
        await session.execute(
            select(TaskVerdict).where(TaskVerdict.task_id == task.id)
        )
    ).scalar_one_or_none()
    previous = row.verdict if row is not None else None
    if row is None:
        row = TaskVerdict(task_id=task.id, agent_id=task.agent_id)
        session.add(row)
    row.agent_id = task.agent_id
    row.verdict = verdict
    row.score = score
    row.checks_total = len(informative)
    row.checks_passed = len(passed_checks)
    row.independent_checks = len(independent)
    row.decided_at = _now()
    row.details = {
        "checks": [
            {
                "id": str(check.id),
                "method": check.method.value,
                "status": check.status.value,
                "weight": check.weight,
            }
            for check in checks
        ],
        "failed": [str(check.id) for check in failed_checks],
        "total_weight": round(total_weight, 3),
    }
    await session.flush()

    # Репутация: вердикт по заданию — более сильный сигнал, чем сам факт успеха
    if verdict is Verdict.verified:
        await record_reputation(
            session,
            agent_id=task.agent_id,
            source=ReputationSource.verification_passed,
            delta=1.0,
            reason=f"Результат задания {task.id} проверен",
            dedup_key=f"verdict:{task.id}:verified",
            task_id=task.id,
        )
    elif verdict is Verdict.rejected:
        await record_reputation(
            session,
            agent_id=task.agent_id,
            source=ReputationSource.verification_failed,
            delta=-1.0,
            reason=f"Результат задания {task.id} отклонён проверкой",
            dedup_key=f"verdict:{task.id}:rejected",
            task_id=task.id,
        )

    # Награда за подтверждённый результат (этап 6). Проверка может прийти
    # как до, так и после расчёта по бюджету, поэтому начисление живёт здесь,
    # в момент вердикта, и идемпотентно по (agent_id, task_id).
    if verdict is Verdict.verified:
        from app.services import economy as economy_service

        await economy_service.grant_reward(session, task, verdict=row)

    # Проверенные возможности: обновляем verified_level на основании вердиктов
    await refresh_verified_capabilities(session, task.agent_id)
    logger.debug(
        "verdict_aggregated task=%s verdict=%s previous=%s checks=%s",
        task.id, verdict.value, previous.value if previous else None, len(informative),
    )
    return row


async def refresh_verified_capabilities(
    session: AsyncSession, agent_id: uuid.UUID
) -> list[AgentCapability]:
    """Подтверждает возможности только по проверенным результатам.

    Уровень не может превышать заявленный: система подтверждает, но не
    повышает. Требуется не менее `CAPABILITY_MIN_CHECKS` подтверждённых
    заданий и доля подтверждений не ниже `CAPABILITY_VERIFY_THRESHOLD`.
    """
    totals = dict(
        (
            await session.execute(
                select(TaskVerdict.verdict, func.count())
                .where(TaskVerdict.agent_id == agent_id)
                .group_by(TaskVerdict.verdict)
            )
        ).all()
    )
    verified_count = int(totals.get(Verdict.verified, 0))
    rejected_count = int(totals.get(Verdict.rejected, 0))
    considered = verified_count + rejected_count
    ratio = verified_count / considered if considered else 0.0

    capabilities = list(
        (
            await session.execute(
                select(AgentCapability).where(AgentCapability.agent_id == agent_id)
            )
        ).scalars().all()
    )
    now = _now()
    for capability in capabilities:
        if verified_count >= CAPABILITY_MIN_CHECKS and ratio >= CAPABILITY_VERIFY_THRESHOLD:
            # Подтверждаем не выше заявленного уровня
            capability.verified_level = capability.declared_level
            capability.verified_at = capability.verified_at or now
        else:
            capability.verified_level = None
            capability.verified_at = None
    await session.flush()
    return capabilities


# ── Отзывы ─────────────────────────────────────────────────────────────
async def create_review(
    session: AsyncSession,
    task: Task,
    *,
    actor: User,
    rating: int,
    comment: str = "",
    confirm_result: bool = False,
) -> TaskReview:
    """Отзыв владельца задания. Один отзыв на задание, только по своим заданиям."""
    if task.requester_id != actor.id:
        raise PermissionDeniedError(
            "Отзыв может оставить только владелец задания", code="review_forbidden"
        )
    if task.status is not TaskStatus.succeeded:
        raise ValidationError(
            "Отзыв возможен только по успешно выполненному заданию",
            code="task_not_reviewable",
            details={"status": task.status.value},
        )
    existing = (
        await session.execute(select(TaskReview).where(TaskReview.task_id == task.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Отзыв по этому заданию уже оставлен", code="review_exists")

    review = TaskReview(
        task_id=task.id,
        agent_id=task.agent_id,
        reviewer_id=actor.id,
        rating=rating,
        comment=comment[:4000],
        confirmed_result=confirm_result,
    )
    session.add(review)
    await session.flush()

    # Оценка → вклад в репутацию: 5★ = +1.5, 3★ = 0, 1★ = -1.5
    delta = round((rating - 3) * 0.75, 3)
    if delta != 0:
        await record_reputation(
            session,
            agent_id=task.agent_id,
            source=ReputationSource.review,
            delta=delta,
            reason=f"Отзыв {rating}/5 по заданию {task.id}",
            dedup_key=f"review:{review.id}",
            task_id=task.id,
            review_id=review.id,
        )
    if confirm_result:
        await record_reputation(
            session,
            agent_id=task.agent_id,
            source=ReputationSource.verification_passed,
            delta=1.0,
            reason=f"Владелец подтвердил результат задания {task.id}",
            dedup_key=f"review:{review.id}:confirmed",
            task_id=task.id,
        )
    return review


async def agent_rating_summary(
    session: AsyncSession, agent_id: uuid.UUID
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                func.count(TaskReview.id),
                func.avg(TaskReview.rating),
                func.sum(func.cast(TaskReview.confirmed_result, __import__("sqlalchemy").Integer)),
            ).where(TaskReview.agent_id == agent_id)
        )
    ).one()
    count, average, confirmed = rows
    distribution = dict(
        (
            await session.execute(
                select(TaskReview.rating, func.count())
                .where(TaskReview.agent_id == agent_id)
                .group_by(TaskReview.rating)
            )
        ).all()
    )
    return {
        "agent_id": agent_id,
        "reviews": int(count or 0),
        "average_rating": round(float(average), 2) if average is not None else None,
        "confirmed_results": int(confirmed or 0),
        "distribution": {str(star): int(distribution.get(star, 0)) for star in range(1, 6)},
    }


# ── Апелляции и пересмотр оценок ───────────────────────────────────────
async def open_appeal(
    session: AsyncSession,
    *,
    actor: User,
    agent_id: uuid.UUID,
    reason: str,
    task_id: uuid.UUID | None = None,
    verification_id: uuid.UUID | None = None,
    anomaly_id: uuid.UUID | None = None,
    evidence: dict[str, Any] | None = None,
) -> Appeal:
    """Открывает апелляцию. Подать может владелец агента или админ."""
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if agent.owner_id != actor.id and not actor.is_admin:
        raise PermissionDeniedError(
            "Апелляцию может подать владелец агента", code="appeal_forbidden"
        )
    if not reason.strip():
        raise ValidationError("Нужна причина апелляции", code="appeal_reason_required")
    if task_id is None and verification_id is None and anomaly_id is None:
        raise ValidationError(
            "Укажите задание, проверку или аномалию, которые оспариваются",
            code="appeal_target_required",
        )
    open_exists = (
        await session.execute(
            select(Appeal).where(
                Appeal.agent_id == agent_id,
                Appeal.status == AppealStatus.open,
                Appeal.task_id == task_id,
            )
        )
    ).scalar_one_or_none()
    if open_exists is not None:
        raise ConflictError("Апелляция по этому заданию уже открыта", code="appeal_exists")

    appeal = Appeal(
        agent_id=agent_id,
        opened_by_id=actor.id,
        task_id=task_id,
        verification_id=verification_id,
        anomaly_id=anomaly_id,
        reason=reason[:8000],
        evidence=evidence or {},
        status=AppealStatus.open,
    )
    session.add(appeal)
    await session.flush()
    return appeal


async def resolve_appeal(
    session: AsyncSession,
    appeal_id: uuid.UUID,
    *,
    actor: User,
    accept: bool,
    resolution: str = "",
) -> Appeal:
    """Решение по апелляции (только админ).

    При `accept` оспариваемые вклады в репутацию отзываются (не удаляются —
    помечаются `reversed_at`), вердикт задания пересчитывается без
    дискредитированной проверки, а агенту возвращается доверие.
    """
    if not actor.is_admin:
        raise PermissionDeniedError("Решение по апелляции принимает админ", code="appeal_forbidden")
    appeal = (
        await session.execute(select(Appeal).where(Appeal.id == appeal_id))
    ).scalars().unique().one_or_none()
    if appeal is None:
        raise NotFoundError("Апелляция не найдена", code="appeal_not_found")
    if appeal.status.is_terminal:
        raise ConflictError(
            "Апелляция уже рассмотрена",
            code="appeal_closed",
            details={"status": appeal.status.value},
        )

    now = _now()
    appeal.status = AppealStatus.accepted if accept else AppealStatus.rejected
    appeal.resolution = resolution[:8000]
    appeal.resolved_by_id = actor.id
    appeal.resolved_at = now

    if accept:
        # 1. Отзываем дискредитированную проверку и связанные с ней вклады
        if appeal.verification_id is not None:
            verification = (
                await session.execute(
                    select(TaskVerification).where(TaskVerification.id == appeal.verification_id)
                )
            ).scalars().unique().one_or_none()
            if verification is not None:
                verification.status = VerificationStatus.skipped
                verification.details = {
                    **(verification.details or {}),
                    "revoked_by_appeal": str(appeal.id),
                }
        events = list(
            (
                await session.execute(
                    select(ReputationEvent).where(
                        ReputationEvent.agent_id == appeal.agent_id,
                        ReputationEvent.reversed_at.is_(None),
                        ReputationEvent.task_id == appeal.task_id,
                    )
                    if appeal.task_id is not None
                    else select(ReputationEvent).where(
                        ReputationEvent.agent_id == appeal.agent_id,
                        ReputationEvent.reversed_at.is_(None),
                        ReputationEvent.anomaly_id == appeal.anomaly_id,
                    )
                )
            ).scalars().all()
        )
        for event in events:
            event.reversed_at = now
            event.reversed_by_appeal_id = appeal.id

        # 2. Компенсация: апелляция сама по себе — сигнал доверия
        await record_reputation(
            session,
            agent_id=appeal.agent_id,
            source=ReputationSource.appeal,
            delta=1.0,
            reason=f"Апелляция {appeal.id} удовлетворена",
            dedup_key=f"appeal:{appeal.id}:accepted",
            task_id=appeal.task_id,
        )

        # 3. Снимаем связанную аномалию
        if appeal.anomaly_id is not None:
            anomaly = (
                await session.execute(
                    select(AnomalySignal).where(AnomalySignal.id == appeal.anomaly_id)
                )
            ).scalars().unique().one_or_none()
            if anomaly is not None:
                anomaly.status = AnomalyStatus.dismissed
                anomaly.resolved_at = now

        # 4. Пересчитываем вердикт задания после отзыва проверки
        if appeal.task_id is not None:
            task = (
                await session.execute(select(Task).where(Task.id == appeal.task_id))
            ).scalars().unique().one_or_none()
            if task is not None:
                await aggregate_verdict(session, task)
    else:
        await record_reputation(
            session,
            agent_id=appeal.agent_id,
            source=ReputationSource.appeal,
            delta=-0.5,
            reason=f"Апелляция {appeal.id} отклонена",
            dedup_key=f"appeal:{appeal.id}:rejected",
            task_id=appeal.task_id,
        )

    # Репутация пересчитывается из актуального набора неотозванных событий
    await _recompute_agent_reputation(session, appeal.agent_id)
    await session.flush()
    return appeal


async def list_appeals(
    session: AsyncSession,
    *,
    actor: User,
    agent_id: uuid.UUID | None = None,
    status: AppealStatus | None = None,
    limit: int = 50,
) -> list[Appeal]:
    query = select(Appeal)
    if not actor.is_admin:
        owned = select(Agent.id).where(Agent.owner_id == actor.id)
        query = query.where(Appeal.agent_id.in_(owned))
    if agent_id is not None:
        query = query.where(Appeal.agent_id == agent_id)
    if status is not None:
        query = query.where(Appeal.status == status)
    query = query.order_by(Appeal.created_at.desc()).limit(min(limit, 200))
    return list((await session.execute(query)).scalars().all())


# ── Обнаружение подозрительных действий ────────────────────────────────
async def detect_anomalies(
    session: AsyncSession, agent_id: uuid.UUID
) -> list[AnomalySignal]:
    """Ищет подозрительные паттерны по фактическим данным (без домыслов).

    Проверяются только наблюдаемые признаки:
    * `result_reuse` — одинаковый `result_hash` на разные входы (копипаст);
    * `rapid_results` — медиана длительности задания ниже 150 мс;
    * `failure_spike` — доля отказов > 50% при ≥ 8 заданиях;
    * `self_dealing` — агент проверяет сам себя (по журналу проверок);
    * `review_ring` — 3+ отзыва от одного и того же пользователя разным
      агентам одного владельца (накрутка).
    """
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")

    found: list[AnomalySignal] = []
    now = _now()

    async def _raise_signal(
        anomaly_type: AnomalyType,
        *,
        dedup: str,
        severity: float,
        details: dict[str, Any],
        task_ids: list[str],
    ) -> None:
        existing = (
            await session.execute(
                select(AnomalySignal).where(
                    AnomalySignal.agent_id == agent_id,
                    AnomalySignal.dedup_key == dedup,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        signal = AnomalySignal(
            agent_id=agent_id,
            anomaly_type=anomaly_type,
            status=AnomalyStatus.open,
            severity=max(0.0, min(1.0, severity)),
            details=details,
            task_ids=task_ids,
            detected_at=now,
            dedup_key=dedup,
        )
        session.add(signal)
        await session.flush()
        found.append(signal)

    # 1. Повторное использование результата на разных входах
    reuse_rows = (
        await session.execute(
            select(Task.result_hash, func.count())
            .where(
                Task.agent_id == agent_id,
                Task.status == TaskStatus.succeeded,
                Task.result_hash.is_not(None),
            )
            .group_by(Task.result_hash)
            .having(func.count() > 1)
        )
    ).all()
    if reuse_rows:
        hashes = [row[0] for row in reuse_rows]
        task_ids = list(
            (
                await session.execute(
                    select(Task.id).where(
                        Task.agent_id == agent_id, Task.result_hash.in_(hashes)
                    )
                )
            ).scalars().all()
        )
        await _raise_signal(
            AnomalyType.result_reuse,
            dedup=f"result_reuse:{','.join(sorted(hashes))[:100]}",
            severity=min(0.9, 0.3 + 0.15 * len(hashes)),
            details={"duplicated_hashes": len(hashes), "tasks": len(task_ids)},
            task_ids=[str(item) for item in task_ids],
        )

    # 2. Подозрительно быстрые результаты
    durations = list(
        (
            await session.execute(
                select(Task.started_at, Task.finished_at).where(
                    Task.agent_id == agent_id,
                    Task.status == TaskStatus.succeeded,
                    Task.started_at.is_not(None),
                    Task.finished_at.is_not(None),
                ).limit(200)
            )
        ).all()
    )
    deltas = [
        (finished - started).total_seconds()
        for started, finished in durations
        if finished and started and finished > started
    ]
    if len(deltas) >= 5:
        ordered = sorted(deltas)
        median = ordered[len(ordered) // 2]
        if median < 0.15:
            await _raise_signal(
                AnomalyType.rapid_results,
                dedup="rapid_results",
                severity=0.5,
                details={"median_seconds": round(median, 4), "samples": len(deltas)},
                task_ids=[],
            )

    # 3. Всплеск отказов
    counters = (
        await session.execute(
            select(Agent.completed_tasks, Agent.failed_tasks).where(Agent.id == agent_id)
        )
    ).one()
    completed, failed = int(counters[0] or 0), int(counters[1] or 0)
    total = completed + failed
    if total >= 8 and failed / total > 0.5:
        await _raise_signal(
            AnomalyType.failure_spike,
            dedup=f"failure_spike:{failed}/{total}",
            severity=min(0.9, failed / total),
            details={"completed": completed, "failed": failed, "ratio": round(failed / total, 3)},
            task_ids=[],
        )

    # 4. Самопроверка
    self_checks = int(
        (
            await session.execute(
                select(func.count())
                .select_from(TaskVerification)
                .where(
                    TaskVerification.agent_id == agent_id,
                    TaskVerification.verifier_agent_id == agent_id,
                )
            )
        ).scalar_one()
    )
    if self_checks:
        await _raise_signal(
            AnomalyType.self_dealing,
            dedup="self_dealing",
            severity=0.8,
            details={"self_checks": self_checks},
            task_ids=[],
        )

    # 5. Круг взаимных отзывов: много отзывов от одного пользователя
    ring = (
        await session.execute(
            select(TaskReview.reviewer_id, func.count())
            .where(TaskReview.agent_id == agent_id)
            .group_by(TaskReview.reviewer_id)
            .having(func.count() >= 3)
        )
    ).all()
    if ring:
        await _raise_signal(
            AnomalyType.review_ring,
            dedup=f"review_ring:{','.join(str(row[0]) for row in ring)[:100]}",
            severity=0.4,
            details={"reviewers": len(ring)},
            task_ids=[],
        )

    # Подтверждённая аномалия — вклад в репутацию (отрицательный)
    for signal in found:
        await record_reputation(
            session,
            agent_id=agent_id,
            source=ReputationSource.anomaly_penalty,
            delta=-round(signal.severity, 3),
            reason=f"Аномалия {signal.anomaly_type.value}",
            dedup_key=f"anomaly:{signal.id}",
            anomaly_id=signal.id,
            weight=1.0,
        )
    return found


async def list_anomalies(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    status: AnomalyStatus | None = None,
    limit: int = 50,
) -> list[AnomalySignal]:
    query = select(AnomalySignal)
    if agent_id is not None:
        query = query.where(AnomalySignal.agent_id == agent_id)
    if status is not None:
        query = query.where(AnomalySignal.status == status)
    query = query.order_by(AnomalySignal.created_at.desc()).limit(min(limit, 200))
    return list((await session.execute(query)).scalars().all())


async def resolve_anomaly(
    session: AsyncSession,
    anomaly_id: uuid.UUID,
    *,
    actor: User,
    confirm: bool,
) -> AnomalySignal:
    """Админ подтверждает или отклоняет сигнал аномалии."""
    if not actor.is_admin:
        raise PermissionDeniedError("Решение по аномалии принимает админ", code="anomaly_forbidden")
    anomaly = (
        await session.execute(select(AnomalySignal).where(AnomalySignal.id == anomaly_id))
    ).scalars().unique().one_or_none()
    if anomaly is None:
        raise NotFoundError("Сигнал не найден", code="anomaly_not_found")
    if anomaly.status is not AnomalyStatus.open:
        raise ConflictError(
            "Сигнал уже рассмотрен", code="anomaly_closed", details={"status": anomaly.status.value}
        )
    anomaly.status = AnomalyStatus.confirmed if confirm else AnomalyStatus.dismissed
    anomaly.resolved_at = _now()
    if not confirm:
        # Отклонённый сигнал не должен вредить репутации
        event = (
            await session.execute(
                select(ReputationEvent).where(
                    ReputationEvent.anomaly_id == anomaly.id,
                    ReputationEvent.reversed_at.is_(None),
                )
            )
        ).scalars().first()
        if event is not None:
            event.reversed_at = _now()
        await _recompute_agent_reputation(session, anomaly.agent_id)
    await session.flush()
    return anomaly


async def list_verifications(
    session: AsyncSession, task_id: uuid.UUID, *, limit: int = 100
) -> list[TaskVerification]:
    return list(
        (
            await session.execute(
                select(TaskVerification)
                .where(TaskVerification.task_id == task_id)
                .order_by(TaskVerification.created_at)
                .limit(min(limit, 200))
            )
        ).scalars().all()
    )


async def trust_overview(session: AsyncSession) -> dict[str, Any]:
    """Сводка по доверию для дашборда: вердикты, проверки, репутация."""
    verdict_rows = (
        await session.execute(
            select(TaskVerdict.verdict, func.count()).group_by(TaskVerdict.verdict)
        )
    ).all()
    verdicts = {verdict.value: 0 for verdict in Verdict}
    for verdict, count in verdict_rows:
        verdicts[verdict.value] = int(count)

    check_rows = (
        await session.execute(
            select(TaskVerification.method, TaskVerification.status, func.count())
            .group_by(TaskVerification.method, TaskVerification.status)
        )
    ).all()
    checks: dict[str, dict[str, int]] = {}
    for method, status, count in check_rows:
        checks.setdefault(method.value, {})[status.value] = int(count)

    open_appeals = int(
        (
            await session.execute(
                select(func.count()).select_from(Appeal).where(Appeal.status == AppealStatus.open)
            )
        ).scalar_one()
    )
    open_anomalies = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AnomalySignal)
                .where(AnomalySignal.status == AnomalyStatus.open)
            )
        ).scalar_one()
    )
    verified_agents = int(
        (
            await session.execute(
                select(func.count()).select_from(Agent).where(Agent.is_verified.is_(True))
            )
        ).scalar_one()
    )
    total_agents = int(
        (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    )
    verified_capabilities = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AgentCapability)
                .where(AgentCapability.verified_level.is_not(None))
            )
        ).scalar_one()
    )
    return {
        "verdicts": verdicts,
        "checks": checks,
        "open_appeals": open_appeals,
        "open_anomalies": open_anomalies,
        "verified_agents": verified_agents,
        "total_agents": total_agents,
        "verified_capabilities": verified_capabilities,
    }


__all__ = [
    "CAPABILITY_MIN_CHECKS",
    "CAPABILITY_VERIFY_THRESHOLD",
    "SOURCE_WEIGHTS",
    "agent_rating_summary",
    "detect_anomalies",
    "list_anomalies",
    "list_appeals",
    "list_verifications",
    "open_appeal",
    "resolve_anomaly",
    "resolve_appeal",
    "trust_overview",
    "aggregate_verdict",
    "compute_reputation",
    "create_review",
    "evaluate_assertions",
    "on_task_finished",
    "record_reputation",
    "refresh_verified_capabilities",
    "reputation_breakdown",
    "request_peer_verification",
    "run_automatic_checks",
    "submit_human_verification",
    "validate_against_schema",
]
