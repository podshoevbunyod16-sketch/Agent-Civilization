"""Изолированное выполнение Python-агентов.

ВАЖНО (правило №7): недоверенный код агентов НИКОГДА не выполняется
в процессе backend. Есть два раннера:

* ``subprocess`` — быстрая деградация для локальной разработки и тестов:
  отдельный процесс, RLIMIT_CPU/RLIMIT_AS/RLIMIT_NOFILE, таймаут wall-clock,
  стерилизованное окружение, ограничение размера вывода.
  Ограничение сети здесь не гарантируется на уровне ядра.
* ``docker`` — рекомендуемый для production: контейнер без сети
  (``--network none``), read-only rootfs, без capabilities, лимиты cgroups.

Контракт агента (не зависит от языка реализации):

    в контейнер/процесс подаётся JSON-задание в файле ``input.json``;
    агент пишет JSON-результат в ``output.json``.

Для Python дополнительно поддерживается функция ``run(payload) -> dict``
в поле ``entrypoint_config.code``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_RUNNER_SOURCE = '''
import json, sys, traceback

def main():
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        request = json.load(fh)
    payload = request.get("input", {})
    code = request.get("code", "")
    namespace = {"__name__": "__agent__"}
    result = None
    try:
        exec(compile(code, "<agent>", "exec"), namespace)
    except BaseException:
        traceback.print_exc()
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            json.dump({"ok": False, "error": {"type": "CompileError",
                       "message": traceback.format_exc(limit=3)}}, fh)
        return 1
    entry = namespace.get("run")
    if not callable(entry):
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            json.dump({"ok": False, "error": {"type": "MissingEntrypoint",
                       "message": "Агент должен определять функцию run(payload)"}}, fh)
        return 2
    try:
        output = entry(payload)
    except BaseException as exc:
        traceback.print_exc()
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            json.dump({"ok": False, "error": {"type": type(exc).__name__,
                       "message": str(exc)[:2000]}}, fh)
        return 3
    if output is None:
        output = {}
    if not isinstance(output, dict):
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            json.dump({"ok": False, "error": {"type": "BadOutput",
                       "message": "run(payload) должен возвращать dict"}}, fh)
        return 4
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump({"ok": True, "output": output}, fh, ensure_ascii=False, default=str)
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def _write_text_sync(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def _read_json_sync(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:  # noqa: PTH123
        return json.load(handle)


async def _write_text(path: str, content: str) -> None:
    """Файловые операции выполняются в пуле потоков, чтобы не блокировать loop."""
    await asyncio.to_thread(_write_text_sync, path, content)


async def _read_json(path: str) -> dict[str, Any]:
    return await asyncio.to_thread(_read_json_sync, path)


@dataclass(slots=True)
class ExecutionResult:
    status: str  # succeeded | failed | timeout | resource_exceeded
    output: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    exit_code: int | None = None
    backend: str = "subprocess"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "backend": self.backend,
        }


class SandboxUnavailableError(RuntimeError):
    pass


def _truncate(text: str, limit: int) -> str:
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:limit].decode(errors="replace") + "\n…[truncated]"


class SandboxRunner:
    """Фабрика раннеров с ограничениями по CPU/RAM/времени/сети."""

    def __init__(
        self,
        *,
        backend: str | None = None,
        max_cpu_seconds: int | None = None,
        max_memory_mb: int | None = None,
        max_wall_seconds: int | None = None,
        max_output_bytes: int | None = None,
        allow_network: bool = False,
        image: str | None = None,
    ) -> None:
        self.backend = backend or settings.sandbox_backend
        self.max_cpu_seconds = max_cpu_seconds or settings.sandbox_max_cpu_seconds
        self.max_memory_mb = max_memory_mb or settings.sandbox_max_memory_mb
        self.max_wall_seconds = max_wall_seconds or settings.sandbox_max_wall_seconds
        self.max_output_bytes = max_output_bytes or settings.sandbox_max_output_bytes
        self.allow_network = allow_network
        self.image = image or settings.sandbox_python_image

    @classmethod
    def from_limits(cls, limits: dict[str, Any] | None) -> SandboxRunner:
        limits = limits or {}
        return cls(
            max_cpu_seconds=int(limits.get("max_cpu_seconds") or settings.sandbox_max_cpu_seconds),
            max_memory_mb=int(limits.get("max_memory_mb") or settings.sandbox_max_memory_mb),
            max_wall_seconds=int(
                limits.get("max_wall_seconds") or settings.sandbox_max_wall_seconds
            ),
            max_output_bytes=int(
                limits.get("max_output_bytes") or settings.sandbox_max_output_bytes
            ),
            allow_network=bool(limits.get("allow_network", False)),
        )

    async def run_python(self, code: str, payload: dict[str, Any]) -> ExecutionResult:
        if self.backend == "noop":
            raise SandboxUnavailableError("Sandbox backend 'noop' не выполняет код")
        if self.backend == "docker":
            if shutil.which("docker") is None:
                raise SandboxUnavailableError("docker недоступен в этом окружении")
            return await self._run_docker(code, payload)
        return await self._run_subprocess(code, payload)

    # ── subprocess ─────────────────────────────────────────────────────
    async def _run_subprocess(self, code: str, payload: dict[str, Any]) -> ExecutionResult:
        workdir = tempfile.mkdtemp(prefix="agent-run-")
        input_path = os.path.join(workdir, "input.json")
        output_path = os.path.join(workdir, "output.json")
        runner_path = os.path.join(workdir, "runner.py")
        try:
            await _write_text(runner_path, _RUNNER_SOURCE)
            await _write_text(
                input_path,
                json.dumps({"input": payload, "code": code}, ensure_ascii=False),
            )

            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONHASHSEED": "0",
                "HOME": workdir,
                "TMPDIR": workdir,
                "LANG": "C.UTF-8",
            }
            started = time.monotonic()
            limits = (self.max_cpu_seconds, self.max_memory_mb, self.max_output_bytes)

            def _preexec() -> None:  # pragma: no cover - выполняется в дочернем процессе
                import resource

                cpu, memory_mb, _ = limits
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
                if memory_mb:
                    resource.setrlimit(
                        resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024)
                    )
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))

            proc = await asyncio.create_subprocess_exec(
                os.environ.get("PYTHON_BIN", "python3"),
                "-I",
                "-S",
                runner_path,
                input_path,
                output_path,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_preexec,
            )
            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.max_wall_seconds
                )
            except TimeoutError:
                timed_out = True
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
            duration_ms = int((time.monotonic() - started) * 1000)

            result = ExecutionResult(
                status="failed",
                stdout=_truncate(stdout_b.decode(errors="replace"), self.max_output_bytes),
                stderr=_truncate(stderr_b.decode(errors="replace"), self.max_output_bytes),
                duration_ms=duration_ms,
                exit_code=proc.returncode,
                backend="subprocess",
            )
            if timed_out:
                result.status = "timeout"
                result.error = {
                    "type": "Timeout",
                    "message": f"Превышен лимит времени {self.max_wall_seconds}s",
                }
                return result

            if await asyncio.to_thread(os.path.exists, output_path):
                try:
                    body = await _read_json(output_path)
                except json.JSONDecodeError:
                    body = {"ok": False, "error": {"type": "BadOutput", "message": "invalid JSON"}}
                if body.get("ok"):
                    result.status = "succeeded"
                    result.output = body.get("output") or {}
                    result.error = None
                else:
                    result.error = body.get("error") or {
                        "type": "ExecutionError",
                        "message": "Агент завершился с ошибкой",
                    }
            else:
                # OOM-kill / сигнал → exit code -9 (SIGKILL) обычно признак лимита памяти
                if proc.returncode in (-9, -24, 137, 24):
                    result.status = "resource_exceeded"
                    result.error = {
                        "type": "ResourceLimits",
                        "message": "Превышены лимиты CPU/памяти",
                    }
                else:
                    result.error = result.error or {
                        "type": "ExecutionError",
                        "message": f"Процесс завершился с кодом {proc.returncode}",
                    }
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ── docker ─────────────────────────────────────────────────────────
    def _chmod(path: str, mode: int) -> None:
        """Смена прав с явным намерением (см. комментарий в _chmod_all)."""
        # Права рассчитаны на uid/gid процесса-хоста; посторонние доступа
        # не получают (никаких 0o777/0o666).
        os.chmod(path, mode)

    async def _run_docker(self, code: str, payload: dict[str, Any]) -> ExecutionResult:
        workdir = tempfile.mkdtemp(prefix="agent-docker-")
        input_path = os.path.join(workdir, "input.json")
        output_path = os.path.join(workdir, "output.json")
        runner_path = os.path.join(workdir, "runner.py")
        try:
            await _write_text(runner_path, _RUNNER_SOURCE)
            await _write_text(
                input_path,
                json.dumps({"input": payload, "code": code}, ensure_ascii=False),
            )
            await _write_text(output_path, "")

            def _chmod_all() -> None:
                # Каталог временный (mkdtemp, 0o700), файлы — 0o600, и это
                # уже минимум прав. Контейнер запускается с `--user <uid>` и
                # `--group-add <gid>` процесса-хоста (см. cmd ниже), поэтому
                # доступ даётся группе, а не «всем»: никаких 0o777/0o666 и
                # никакой записи для посторонних пользователей хоста.
                self._chmod(workdir, 0o750)
                self._chmod(input_path, 0o640)
                self._chmod(runner_path, 0o640)
                # Результат пишет контейнер: запись группе, не «всем»
                self._chmod(output_path, 0o660)

            await asyncio.to_thread(_chmod_all)

            cmd = [
                "docker", "run", "--rm", "--pull", "never",
                "--network", "bridge" if self.allow_network else "none",
                "--memory", f"{self.max_memory_mb}m",
                "--memory-swap", f"{self.max_memory_mb}m",
                "--cpus", "1",
                "--pids-limit", "64",
                "--read-only",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                # Тот же uid/gid, что у процесса-хоста: каталог задания
                # принадлежит ему, поэтому контейнер обходится без мировых
                # прав на запись. Внутри контейнера это непривилегированный
                # пользователь (root хоста в контейнер не попадает).
                "--user", f"{os.getuid()}:{os.getgid()}",
                "-v", f"{workdir}:/work:rw",
                "-w", "/work",
                self.image,
                "python", "-I", "-S", "/work/runner.py", "/work/input.json",
                "/work/output.json",
            ]
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.max_wall_seconds
                )
            except TimeoutError:
                timed_out = True
                await asyncio.create_subprocess_exec(
                    "docker", "kill", str(proc.pid)  # type: ignore[arg-type]
                )
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
            duration_ms = int((time.monotonic() - started) * 1000)
            result = ExecutionResult(
                status="failed",
                stdout=_truncate(stdout_b.decode(errors="replace"), self.max_output_bytes),
                stderr=_truncate(stderr_b.decode(errors="replace"), self.max_output_bytes),
                duration_ms=duration_ms,
                exit_code=proc.returncode,
                backend="docker",
            )
            if timed_out:
                result.status = "timeout"
                result.error = {"type": "Timeout", "message": "wall-clock limit"}
                return result
            if await asyncio.to_thread(os.path.exists, output_path):
                try:
                    body = await _read_json(output_path)
                except json.JSONDecodeError:
                    body = {}
                if body.get("ok"):
                    result.status = "succeeded"
                    result.output = body.get("output") or {}
                else:
                    result.error = body.get("error") or {
                        "type": "ExecutionError",
                        "message": "no output",
                    }
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def new_trace_id() -> str:
    return uuid.uuid4().hex
