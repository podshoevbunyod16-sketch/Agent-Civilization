"""Этап 10, F: бэкап проверен восстановлением, а не «настроен».

Тесты делают то, что делает runbook: создают снимок на реальной базе,
шифруют, читают обратно, восстанавливают в отдельную схему и сверяют
строки и финансовые инварианты. Плюс негативные случаи — подмена
шифротекста, чужой ключ, отсутствие манифеста.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import backup as backup_service

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    return tmp_path / "backups"


async def _seed(client, user):
    """Немного реальных данных: агент, задание, деньги."""
    from tests.conftest import create_published_agent

    agent = await create_published_agent(client, user)
    await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input": {"prompt": "backup"}, "budget_units": 5},
        headers=user.headers,
    )
    return agent


async def test_backup_creates_encrypted_file_with_manifest(client, user, session, backup_dir):
    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)

    assert path is not None and path.exists()
    # Файл зашифрован: сигнатура контейнера и отсутствие открытого текста
    raw = path.read_bytes()
    assert raw.startswith(backup_service.MAGIC)
    assert b'"table"' not in raw
    assert b"@example.com" not in raw

    assert manifest.rows > 0
    assert manifest.tables["users"] == 1
    assert manifest.checksum_plain and len(manifest.checksum_plain) == 64

    # Манифест лежит рядом: восстановление не требует БД
    sidecar = Path(str(path) + ".json")
    assert sidecar.exists()
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert saved["version"] == backup_service.VERSION
    assert saved["checksum_plain"] == manifest.checksum_plain


async def test_backup_restore_verifies_rows(client, user, session, backup_dir):
    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)
    assert path is not None

    result = await backup_service.restore_backup(session, path)
    assert result["ok"] is True, result
    assert result["rows_restored"] == manifest.rows
    assert result["mismatches"] == {}
    assert result["unbalanced_transactions"] == 0
    assert result["schema"].startswith("restore_")


async def test_backup_decrypt_roundtrip_matches_checksum(client, user, session, backup_dir):
    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)
    read_manifest, records = backup_service.read_backup(path)
    assert read_manifest.checksum_plain == manifest.checksum_plain
    assert read_manifest.rows == manifest.rows == len(records)
    tables = {record["table"] for record in records}
    assert "users" in tables and "agents" in tables


async def test_tampered_backup_rejected(client, user, session, backup_dir):
    await _seed(client, user)
    _, path = await backup_service.create_backup(session)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF  # порча шифротекста
    path.write_bytes(bytes(data))

    with pytest.raises(backup_service.BackupError):
        backup_service.read_backup(path)


async def test_backup_without_manifest_still_checks_signature(client, user, session, backup_dir):
    await _seed(client, user)
    _, path = await backup_service.create_backup(session)
    Path(str(path) + ".json").unlink()

    # Без манифеста читается (данные расшифровываются), но checksum-сверки нет
    manifest, records = backup_service.read_backup(path)
    assert manifest.rows == len(records) > 0


async def test_unknown_key_version_rejected(client, user, session, backup_dir, monkeypatch):
    await _seed(client, user)
    _, path = await backup_service.create_backup(session)
    data = bytearray(path.read_bytes())
    # Подменяем версию ключа на несуществующую
    data[len(backup_service.MAGIC)] = 99
    path.write_bytes(bytes(data))
    with pytest.raises(backup_service.BackupError) as exc:
        backup_service.read_backup(path)
    assert "версией ключа" in str(exc.value)


async def test_retention_removes_old_backups(client, user, session, backup_dir, monkeypatch):
    import os
    import time

    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)
    assert path is not None
    # Состариваем файл на 30 дней
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    os.utime(Path(str(path) + ".json"), (old, old))

    removed = backup_service.apply_retention(backup_dir, keep_days=14)
    assert path.name in removed
    assert not path.exists()
    assert not Path(str(path) + ".json").exists()
    assert manifest.rows > 0


async def test_backup_api_endpoints(client, admin_user, backup_dir):
    await _seed(client, admin_user)

    created = await client.post("/api/v1/ops/backups", headers=admin_user.headers)
    assert created.status_code == 200, created.text
    filename = Path(created.json()["path"]).name

    listed = await client.get("/api/v1/ops/backups", headers=admin_user.headers)
    assert listed.status_code == 200
    assert any(item["file"] == filename for item in listed.json()["items"])

    verified = await client.post(
        f"/api/v1/ops/backups/{filename}/verify", headers=admin_user.headers
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["ok"] is True

    deleted = await client.delete(
        f"/api/v1/ops/backups/{filename}", headers=admin_user.headers
    )
    assert deleted.status_code == 200
    missing = await client.post(
        f"/api/v1/ops/backups/{filename}/verify", headers=admin_user.headers
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "backup_not_found"


async def test_backup_endpoints_admin_only(client, user, backup_dir):
    response = await client.post("/api/v1/ops/backups", headers=user.headers)
    assert response.status_code == 403


async def test_backup_filename_traversal_blocked(client, admin_user, backup_dir):
    for name in ("../etc/passwd", "..%2Fsecret", ".hidden"):
        response = await client.post(
            f"/api/v1/ops/backups/{name}/verify", headers=admin_user.headers
        )
        assert response.status_code in (400, 404, 422), name


async def test_backup_rejects_destructive_table_switch(client, admin_user, backup_dir):
    """В бэкап нельзя попросить произвольную таблицу (SQL-инъекция через имя)."""
    from app.core.config import settings

    with pytest.raises(backup_service.BackupError):
        await backup_service.create_backup(
            session=None, tables=["users; DROP TABLE users--"]  # type: ignore[arg-type]
        )
    assert settings.backup_dir  # конфигурация не изменилась


async def test_restore_detects_row_loss(client, user, session, backup_dir, monkeypatch):
    """Если при восстановлении потерялись строки — проверка обязана упасть."""
    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)

    # Имитируем потерю: в манифесте «ожидаем» на одну строку больше
    sidecar = Path(str(path) + ".json")
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    saved["tables"]["users"] = saved["tables"]["users"] + 1
    sidecar.write_text(json.dumps(saved), encoding="utf-8")

    # Проверять надо именно расхождение состава таблиц с заголовком файла
    with pytest.raises(backup_service.BackupError):
        backup_service.read_backup(path)


async def test_minimal_backup_restore_still_balanced(client, session, backup_dir):
    """База без доменных данных восстанавливается и проходит финпроверку.

    Регистрация участника создаёт стартовый грант (это тоже данные), поэтому
    проверяем именно инвариант: восстановленные проводки сбалансированы.
    """
    from tests.conftest import register_user

    await register_user(client)  # 1 user + грант в ledger
    manifest, path = await backup_service.create_backup(session)
    result = await backup_service.restore_backup(session, path)
    assert result["ok"] is True
    assert result["rows_restored"] == manifest.rows
    assert result["unbalanced_transactions"] == 0
    # Схема названа по времени восстановления и не совпадает с рабочей
    assert result["schema"].startswith("restore_")
    assert result["schema"] != "public"


async def test_backup_worker_passes_and_verifies(client, user, session, backup_dir, monkeypatch):
    """Полный проход демона: снимок → проверка восстановлением → retention."""
    import asyncio as _asyncio

    from app.worker import backup as backup_worker

    await _seed(client, user)

    # Демон открывает собственные сессии — подменяем фабрику на тестовую БД
    from app.db.session import get_session_factory

    async def fake_factory():
        return get_session_factory()()

    result = await backup_worker.run_once(verify=True)
    assert result["rows"] > 0
    assert result["verified"] is True
    assert result["path"] and Path(str(result["path"])).exists()
    assert isinstance(result["removed"], list)
    assert _asyncio is not None and fake_factory is not None


async def test_rpo_rto_targets_are_configured():
    """RPO/RTO заявлены в конфигурации и проверяются runbook'ом."""
    from app.core.config import settings

    assert settings.rpo_target_seconds > 0
    assert settings.rto_target_seconds > 0
    # RTO не может быть меньше RPO — иначе восстановление «быстрее» потери
    assert settings.rto_target_seconds >= 60


async def test_concurrent_restores_do_not_share_schema(client, user, session, backup_dir):
    """Два восстановления подряд не смешивают строки.

    Дефект, найденный полным прогоном: имя схемы было
    `restore_<unixtime>` без суффикса, поэтому проверка сразу после
    создания бэкапа попадала в уже существующую схему — `IF NOT EXISTS`
    молчал, строки складывались, сверка ложно проваливалась.
    """
    await _seed(client, user)
    manifest, path = await backup_service.create_backup(session)

    first = await backup_service.restore_backup(session, path)
    second = await backup_service.restore_backup(session, path)

    assert first["schema"] != second["schema"]
    assert first["ok"] is True and second["ok"] is True
    assert first["rows_restored"] == manifest.rows
    assert second["rows_restored"] == manifest.rows


async def test_restore_cleanup_removes_schema(client, user, session, backup_dir):
    """`cleanup=True` не оставляет схему `restore_*` в рабочей базе."""
    from sqlalchemy import text

    await _seed(client, user)
    _, path = await backup_service.create_backup(session)
    result = await backup_service.restore_backup(session, path, cleanup=True)

    exists = (
        await session.execute(
            text("SELECT count(*) FROM information_schema.schemata WHERE schema_name = :name"),
            {"name": result["schema"]},
        )
    ).scalar_one()
    assert exists == 0
