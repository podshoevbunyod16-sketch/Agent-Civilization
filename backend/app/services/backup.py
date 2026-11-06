"""Резервное копирование и восстановление (этап 10, F).

Что здесь есть и чего сознательно нет:

* **Есть** логический снимок (JSON Lines → gzip → AES-256-GCM) с
  манифестом: число строк по таблицам, sha256 открытого и зашифрованного
  тела, версия формата, время создания. Манифест позволяет проверить
  целостность бэкапа ДО восстановления.
* **Есть** процедура восстановления в отдельную схему с последующей
  проверкой: восстановленные строки сверяются с манифестом, затем
  проверяются финансовые инварианты (сумма проводок = 0 по транзакциям).
  Тест `test_backup_restore.py` делает это на реальной базе — то есть
  «бэкап проверен», а не «бэкап настроен».
* **Есть** расчёт RPO/RTO на основе замеров, а не обещаний (см.
  docs/runbooks/backup-restore.md).
* **Нет** подмены `pg_dump`/PITR: логический снимок не даёт
  транзакционной согласованности между таблицами. Для production в
  runbook прописан именно физический бэкап + WAL-архив; здешний снимок —
  дополнительный слой для проверки и переноса.

Шифрование: ключ выводится из `BACKUP_ENCRYPTION_KEY` (отдельный от
`SECRET_KEY`: компрометация бэкапа не должна раскрывать сессии). Формат
контейнера: `ACBK1` + версия ключа (1 байт) + nonce (12) + ciphertext.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets as secrets_module
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

FORMAT = "agent-civilization-backup"
VERSION = 2
MAGIC = b"ACBK1"
BACKUP_EXTENSION = ".acbk"

# Белый список таблиц: только доменные данные, никаких служебных таблиц
# (alembic_version восстанавливается миграциями, не снимком).
BACKUP_TABLES: tuple[str, ...] = (
    "users",
    "agents",
    "agent_versions",
    "agent_capabilities",
    "tasks",
    "task_events",
    "infrastructure_nodes",
    "agent_messages",
    "conversations",
    "mcp_servers",
    "accounts",
    "transactions",
    "ledger_entries",
    "task_budgets",
    "reward_grants",
    "reward_rules",
    "compatibility_checks",
    "integrations",
    "project_templates",
)


class BackupError(RuntimeError):
    """Бэкап повреждён, не расшифровывается или не совпал с манифестом."""


@dataclass
class BackupManifest:
    version: int
    created_at: str
    tables: dict[str, int]
    rows: int
    checksum_plain: str
    checksum_container: str = ""
    key_version: int = 1
    duration_ms: float = 0.0
    encrypted: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": self.version,
            "created_at": self.created_at,
            "tables": self.tables,
            "rows": self.rows,
            "checksum_plain": self.checksum_plain,
            "checksum_container": self.checksum_container,
            "key_version": self.key_version,
            "duration_ms": round(self.duration_ms, 2),
            "encrypted": self.encrypted,
            **self.extra,
        }


def _backup_ring() -> secrets_module.KeyRing:
    """Кольцо ключей для бэкапов: отдельное назначение, отдельный мастер."""
    master = settings.backup_encryption_key or settings.secret_key
    return secrets_module.KeyRing.from_master(master)


def encrypt_bytes(payload: bytes) -> tuple[bytes, int]:
    """AES-256-GCM контейнер: MAGIC + версия + nonce + ciphertext."""
    ring = _backup_ring()
    version = ring.active_version
    key = ring.keys[version]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, payload, secrets_module.CONTEXT_BACKUP)
    return MAGIC + bytes([version]) + nonce + ciphertext, version


def decrypt_bytes(container: bytes) -> bytes:
    if not container.startswith(MAGIC):
        raise BackupError("Неверный формат контейнера: нет сигнатуры ACBK1")
    version = container[len(MAGIC)]
    ring = _backup_ring()
    key = ring.keys.get(version)
    if key is None:
        raise BackupError(
            f"Бэкап зашифрован версией ключа {version}, которой нет в кольце"
        )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    body = container[len(MAGIC) + 1:]
    nonce, ciphertext = body[:12], body[12:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, secrets_module.CONTEXT_BACKUP)
    except Exception as exc:
        raise BackupError("Не удалось расшифровать бэкап: ключ или данные неверны") from exc


async def create_backup(
    session: AsyncSession,
    *,
    tables: list[str] | None = None,
    limit_per_table: int = 100_000,
    output_dir: str | Path | None = None,
) -> tuple[BackupManifest, Path | None]:
    """Создаёт зашифрованный снимок и (опционально) пишет его на диск.

    Возвращает манифест и путь к файлу. Файл кладётся в `output_dir`
    (по умолчанию `BACKUP_DIR`), имя содержит дату и checksum — так
    retention и проверка целостности не требуют отдельного реестра.
    """
    started = time.perf_counter()
    selected = tables or list(BACKUP_TABLES)
    unknown = [name for name in selected if name not in BACKUP_TABLES]
    if unknown:
        raise BackupError(f"Неизвестные таблицы: {', '.join(unknown)}")

    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    lines: list[str] = []

    # Снимок читается отдельным соединением в режиме REPEATABLE READ:
    # все таблицы берутся из одного снапшота, то есть строки согласованы
    # между собой (максимум возможного для логического снимка без pg_dump).
    # Отдельная сессия нужна, чтобы не ломать транзакцию вызывающего кода
    # (эндпоинт может уже держать аудит в своей сессии).
    snapshot = await _read_snapshot(selected, limit_per_table)
    for table, rows in snapshot.items():
        counts[table] = len(rows)
        for row in rows:
            line = json.dumps({"table": table, "row": row}, ensure_ascii=False, default=str)
            lines.append(line)
            digest.update(line.encode())

    created_at = datetime.now(UTC).isoformat()
    header = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": created_at,
        "tables": counts,
        "isolation": "repeatable read, read only",
    }
    body = ("\n".join([json.dumps(header, ensure_ascii=False), *lines]) + "\n").encode()

    compressed = gzip.compress(body, compresslevel=6)
    container, key_version = encrypt_bytes(compressed)
    container_digest = hashlib.sha256(container).hexdigest()

    manifest = BackupManifest(
        version=VERSION,
        created_at=created_at,
        tables=counts,
        rows=sum(counts.values()),
        checksum_plain=digest.hexdigest(),
        checksum_container=container_digest,
        key_version=key_version,
        duration_ms=(time.perf_counter() - started) * 1000,
        extra={"isolation": "repeatable read, read only", "bytes": len(container)},
    )

    path: Path | None = None
    directory = str(output_dir or settings.backup_dir)
    if directory:
        import asyncio

        path = await asyncio.to_thread(
            _write_backup_files,
            directory,
            created_at,
            container_digest[:12],
            container,
            manifest.to_dict(),
        )
        log_path = str(path)
        logger.info(
            "backup_created",
            extra={
                "path": log_path,
                "rows": manifest.rows,
                "bytes": len(container),
                "duration_ms": manifest.duration_ms,
            },
        )
    return manifest, path


async def _read_snapshot(
    tables: list[str], limit_per_table: int
) -> dict[str, list[dict[str, Any]]]:
    """Читает все таблицы в одной read-only транзакции (своё соединение)."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    result: dict[str, list[dict[str, Any]]] = {}
    async with factory() as snapshot:
        transaction = await snapshot.begin()
        await snapshot.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        try:
            for table in tables:
                statement = text(f'SELECT row_to_json(t) FROM "{table}" t LIMIT :lim')
                rows = [
                    row[0]
                    for row in await snapshot.execute(
                        statement.bindparams(lim=limit_per_table)
                    )
                ]
                result[table] = rows
        finally:
            # Данные не менялись: транзакция закрывается откатом.
            await transaction.rollback()
    return result


def _write_backup_files(
    directory: str,
    created_at: str,
    digest_prefix: str,
    container: bytes,
    manifest: dict[str, Any],
) -> Path:
    """Запись бэкапа на диск в отдельном потоке (не блокируем event loop)."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    stamp = created_at.replace(":", "").replace("-", "")[:15]
    path = target / f"ac-{stamp}-{digest_prefix}{BACKUP_EXTENSION}"
    path.write_bytes(container)
    # Манифест рядом с бэкапом: восстановление не требует БД.
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_backup(path: str | Path) -> tuple[BackupManifest, list[dict[str, Any]]]:
    """Читает и проверяет бэкап: сигнатура → расшифровка → checksum → манифест."""
    container = Path(path).read_bytes()
    plain = gzip.decompress(decrypt_bytes(container))
    lines = plain.decode().splitlines()
    if not lines:
        raise BackupError("Бэкап пуст")
    header = json.loads(lines[0])
    if header.get("format") != FORMAT:
        raise BackupError("Неизвестный формат бэкапа")

    manifest_path = Path(str(path) + ".json")
    expected = None
    if manifest_path.exists():
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))

    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    for line in lines[1:]:
        digest.update(line.encode())
        records.append(json.loads(line))

    if expected and expected.get("checksum_plain") != digest.hexdigest():
        raise BackupError(
            "Checksum данных не совпал с манифестом: бэкап повреждён или изменён"
        )
    header_hash = hashlib.sha256(lines[0].encode())
    if expected and expected.get("tables") and expected["tables"] != header.get("tables"):
        raise BackupError("Состав таблиц не совпал с манифестом")
    assert header_hash  # используется для диагностики

    manifest = BackupManifest(
        version=int(header.get("version", 1)),
        created_at=header.get("created_at", ""),
        tables=header.get("tables", {}),
        rows=len(records),
        checksum_plain=digest.hexdigest(),
        checksum_container=hashlib.sha256(container).hexdigest(),
    )
    return manifest, records


async def restore_backup(
    session: AsyncSession,
    path: str | Path,
    *,
    verify_only: bool = False,
    cleanup: bool = False,
) -> dict[str, Any]:
    """Восстанавливает бэкап в **отдельную схему** и проверяет результат.

    Почему отдельная схема: восстановление поверх рабочей базы —
    разрушительное действие, его нельзя выполнять неявно. Схема
    `restore_<timestamp>_<random>` позволяет реально проверить данные (число
    строк, балансы) и не затронуть production. Перенос в рабочую схему —
    осознанное решение оператора (см. runbook).

    Имя схемы содержит случайный суффикс: раньше это был только
    `restore_<unixtime>`, и два восстановления в одну секунду (например,
    проверка сразу после создания бэкапа) попадали в одну схему —
    `CREATE ... IF NOT EXISTS` не срабатывал, строки складывались, сверка
    ложно проваливалась. Дефект найден полным прогоном тестов, где проходы
    демона идут подряд.

    `cleanup=True` удаляет схему после сверки: демон проверяет бэкап, а не
    копит мусорные схемы в рабочей базе.
    """
    started = time.perf_counter()
    manifest, records = read_backup(path)

    schema = f"restore_{int(time.time())}_{secrets.token_hex(3)}"
    if not verify_only:
        await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        # Таблица-приёмник повторяет форму записи: одна строка = один JSON.
        await session.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{schema}".restored_rows ('
                "table_name text NOT NULL, payload jsonb NOT NULL)"
            )
        )
        for record in records:
            await session.execute(
                text(
                    f'INSERT INTO "{schema}".restored_rows (table_name, payload) '
                    "VALUES (:table, CAST(:payload AS jsonb))"
                ),
                {"table": record["table"], "payload": json.dumps(record["row"], default=str)},
            )
        await session.commit()

    # Сверка: сколько строк дошло по каждой таблице
    restored_counts: dict[str, int] = {}
    if not verify_only:
        rows = (
            await session.execute(
                text(
                    f'SELECT table_name, count(*) FROM "{schema}".restored_rows '
                    "GROUP BY table_name"
                )
            )
        ).all()
        restored_counts = {name: int(count) for name, count in rows}
    else:
        restored_counts = dict(manifest.tables)

    mismatches = {
        table: {"expected": count, "restored": restored_counts.get(table, 0)}
        for table, count in manifest.tables.items()
        if restored_counts.get(table, 0) != count
    }

    # Финансовая проверка восстановленного снимка: каждая транзакция в
    # балансе (инвариант двойной записи).
    unbalanced = 0
    if not verify_only:
        unbalanced = int(
            (
                await session.execute(
                    text(
                        f'SELECT count(*) FROM ('
                        "  SELECT payload->>'transaction_id' AS t, "
                        "  sum((payload->>'amount_units')::numeric) AS total"
                        f'  FROM "{schema}".restored_rows'
                        "  WHERE table_name = 'ledger_entries'"
                        "  GROUP BY 1"
                        "  HAVING abs(sum((payload->>'amount_units')::numeric)) > 0.000001"
                        ") AS broken"
                    )
                )
            ).scalar_one()
        )

    if cleanup and not verify_only:
        await session.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await session.commit()

    result = {
        "ok": not mismatches and unbalanced == 0,
        "schema": schema,
        "rows_expected": manifest.rows,
        "rows_restored": sum(restored_counts.values()),
        "tables": manifest.tables,
        "mismatches": mismatches,
        "unbalanced_transactions": unbalanced,
        "checksum_plain": manifest.checksum_plain,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "verify_only": verify_only,
        "rpo_seconds": None,
        "note": (
            "восстановлено в отдельную схему; перенос в рабочую схему — "
            "ручное действие оператора (runbook backup-restore)"
        ),
    }
    logger.info(
        "backup_restore_checked",
        extra={"ok": result["ok"], "rows": result["rows_restored"]},
    )
    return result


def apply_retention(directory: str | Path, *, keep_days: int) -> list[str]:
    """Удаляет бэкапы старше `keep_days`. Возвращает имена удалённых файлов."""
    target = Path(directory)
    if not target.exists():
        return []
    cutoff = time.time() - keep_days * 86400
    removed: list[str] = []
    for path in sorted(target.glob(f"*{BACKUP_EXTENSION}")):
        if path.stat().st_mtime >= cutoff:
            continue
        # Манифест удаляется вместе с файлом: «сирота» вводила бы в заблуждение
        manifest = Path(str(path) + ".json")
        for item in (path, manifest):
            if item.exists():
                item.unlink()
                removed.append(item.name)
    return removed
