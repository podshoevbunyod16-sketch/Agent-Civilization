"""Хелперы для написания агентов и собственных worker-циклов."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from agent_civilization_sdk.client import NodeClient

logger = logging.getLogger(__name__)


def agent_function(func: Callable[[dict[str, Any]], dict[str, Any]]):
    """Декоратор точки входа агента: явный контракт run(payload) -> dict.

        @agent_function
        def run(payload):
            return {"echo": payload}
    """

    @functools.wraps(func)
    def wrapper(payload: dict[str, Any]) -> dict[str, Any]:
        result = func(payload)
        if not isinstance(result, dict):
            raise TypeError("Агент должен возвращать dict")
        return result

    wrapper.__agent_entrypoint__ = True  # type: ignore[attr-defined]
    return wrapper


class AgentWorker:
    """Собственный цикл узла: забирает задания и выполняет их как угодно.

    Платформа не диктует язык: передайте функцию-исполнитель, которая
    получает assignment и возвращает (success, output, error_message).

        def execute(assignment):
            return True, {"ok": True}, None

        AgentWorker(client, execute).run_forever()

    Безопасность: не выполняйте недоверенный код в своём процессе без
    изоляции. Если нужен python-код агента — используйте встроенный
    `python -m app.worker.node_worker`: у него есть песочница.
    """

    def __init__(
        self,
        client: NodeClient,
        execute: Callable[[dict[str, Any]], tuple[bool, dict[str, Any], str | None]],
        *,
        poll_interval: float = 2.0,
        max_tasks: int = 1,
        capabilities: list[str] | None = None,
    ) -> None:
        self.client = client
        self.execute = execute
        self.poll_interval = poll_interval
        self.max_tasks = max_tasks
        self.capabilities = capabilities or ["python"]
        self._running = True
        self.processed = 0

    def stop(self) -> None:
        self._running = False

    def run_once(self) -> int:
        assignments = self.client.claim(self.max_tasks, self.capabilities)
        for assignment in assignments:
            task_id = assignment["task_id"]
            lease = assignment["lease_token"]
            try:
                self.client.progress(task_id, lease, "задание принято")
                success, output, error = self.execute(assignment)
                self.client.result(
                    task_id, lease, success=success, output=output, error_message=error
                )
                self.processed += 1
            except Exception as exc:
                # Сеть/сериализация: задание вернётся в очередь по истечении аренды
                logger.warning("task_failed_to_report task_id=%s error=%s", task_id, exc)
        return len(assignments)

    def run_forever(self) -> None:
        self.client.verify()
        sequence = 0
        while self._running:
            sequence += 1
            try:
                self.client.heartbeat(sequence)
                if not self.run_once():
                    time.sleep(self.poll_interval)
            except Exception as exc:  # pragma: no cover - инфраструктурные сбои
                logger.warning("worker_loop_error error=%s", exc)
                time.sleep(self.poll_interval)
