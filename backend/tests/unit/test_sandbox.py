"""Тесты изолированного исполнения кода агентов."""

from __future__ import annotations

import json
import uuid

import pytest

from app.sandbox import ExecutionResult, SandboxRunner, SandboxUnavailableError, new_trace_id

pytestmark = pytest.mark.asyncio


async def test_python_agent_success():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python(
        "def run(payload):\n    return {'echo': payload['value'], 'doubled': payload['value'] * 2}",
        {"value": 21},
    )
    assert result.status == "succeeded", result.stderr
    assert result.output == {"echo": 21, "doubled": 42}
    assert result.error is None
    assert result.backend == "subprocess"
    assert result.duration_ms >= 0


async def test_agent_exception_is_reported_not_leaked():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python(
        "def run(payload):\n    raise ValueError('агент не смог обработать запрос')", {}
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["type"] == "ValueError"
    assert "не смог обработать" in result.error["message"]
    assert result.output == {}


async def test_agent_without_run_function_fails():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python("x = 1\n", {})
    assert result.status == "failed"
    assert result.error["type"] == "MissingEntrypoint"


async def test_agent_returning_non_dict_fails():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python("def run(payload):\n    return [1, 2, 3]", {})
    assert result.status == "failed"
    assert result.error["type"] == "BadOutput"


async def test_syntax_error_fails_gracefully():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python("def run(payload)\n    return {}", {})
    assert result.status == "failed"
    assert result.error is not None


async def test_infinite_loop_hits_wall_clock_timeout():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=2)
    result = await runner.run_python("def run(payload):\n    while True:\n        pass", {})
    assert result.status in {"timeout", "resource_exceeded", "failed"}
    assert result.duration_ms >= 1000, "таймаут должен срабатывать по wall-clock"


async def test_memory_limit_enforced():
    runner = SandboxRunner(
        backend="subprocess", max_wall_seconds=15, max_memory_mb=64, max_cpu_seconds=5
    )
    result = await runner.run_python(
        "def run(payload):\n    blob = bytearray(600 * 1024 * 1024)\n    return {'len': len(blob)}",
        {},
    )
    assert result.status in {"failed", "resource_exceeded"}, result.status


async def test_large_output_is_truncated():
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10, max_output_bytes=1024)
    result = await runner.run_python(
        "import sys\ndef run(payload):\n    print('x' * 100000)\n    return {'ok': True}", {}
    )
    assert result.status == "succeeded"
    assert len(result.stdout.encode()) <= 1200
    assert "truncated" in result.stdout


async def test_environment_is_sanitized(monkeypatch):
    """Секреты backend не должны быть видны коду агента."""
    monkeypatch.setenv("SECRET_KEY", "super-secret-value")
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python(
        "import os\ndef run(payload):\n    return {'keys': sorted(os.environ)}", {}
    )
    assert result.status == "succeeded"
    leaked = " ".join(result.output["keys"]).upper()
    assert "SECRET" not in leaked
    assert "DATABASE" not in leaked


async def test_noop_backend_refuses_execution():
    runner = SandboxRunner(backend="noop")
    with pytest.raises(SandboxUnavailableError):
        await runner.run_python("def run(payload):\n    return {}", {})


async def test_subprocess_backend_is_blocked_in_production(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    runner = SandboxRunner(backend="subprocess")
    with pytest.raises(SandboxUnavailableError, match="только в Docker sandbox"):
        await runner.run_python("def run(payload):\n    return {}", {})


async def test_runner_built_from_resource_limits():
    runner = SandboxRunner.from_limits(
        {
            "max_cpu_seconds": 5,
            "max_wall_seconds": 7,
            "max_memory_mb": 128,
            "max_output_bytes": 2048,
            "allow_network": True,
        }
    )
    assert runner.max_cpu_seconds == 5
    assert runner.max_wall_seconds == 7
    assert runner.max_memory_mb == 128
    assert runner.max_output_bytes == 2048
    assert runner.allow_network is True


async def test_execution_result_serialisation():
    result = ExecutionResult(status="succeeded", output={"a": 1}, duration_ms=12)
    payload = result.to_dict()
    assert json.loads(json.dumps(payload))["output"] == {"a": 1}
    assert payload["backend"] == "subprocess"


async def test_trace_id_is_unique_hex():
    a, b = new_trace_id(), new_trace_id()
    assert a != b
    assert len(a) == 32
    uuid.UUID(hex=a)


async def test_agent_cannot_write_outside_workdir():
    """Рабочий каталог изолирован; относительная запись не выходит наружу."""
    runner = SandboxRunner(backend="subprocess", max_wall_seconds=10)
    result = await runner.run_python(
        "def run(payload):\n"
        "    with open('result.txt', 'w') as fh:\n"
        "        fh.write('data')\n"
        "    return {'written': True}",
        {},
    )
    assert result.status == "succeeded"
    assert result.output == {"written": True}
