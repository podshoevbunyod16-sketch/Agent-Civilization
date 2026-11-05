"""API-тесты открытой экосистемы (этап 8).

Проверяются не «ответы эндпоинтов», а свойства, ради которых этап существует:

* проверка совместимости действительно **исполняет** агента в песочнице и
  ловит несоответствие схем (в том числе негативным тестом);
* чужой агент не проверяется и чужая история проверок не отдаётся;
* каталог интеграций не раскрывает приватные черновики и запрещает
  проверку внутренних адресов (SSRF);
* шаблоны проектов отдаются с рабочим кодом и проходят ту же проверку.
"""

from __future__ import annotations

import uuid

import httpx

from tests.conftest import create_agent, create_published_agent, version_payload

ECHO_CODE = """
def run(payload):
    text = (payload.get("input") or {}).get("text", "")
    return {"echo": text, "length": len(text)}
"""

PYTHON_VERSION = {
    "entrypoint_type": "python",
    "entrypoint_config": {"code": ECHO_CODE, "sample_input": {"input": {"text": "привет"}}},
    "input_schema": {
        "type": "object",
        "required": ["input"],
        "properties": {"input": {"type": "object"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["echo", "length"],
        "properties": {"echo": {"type": "string"}, "length": {"type": "integer"}},
    },
}

async def python_agent(client, user, **version_overrides):
    agent = await create_agent(client, user)
    payload = version_payload(publish=True, **PYTHON_VERSION)
    payload.update(version_overrides)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions", json=payload, headers=user.headers
    )
    assert response.status_code == 201, response.text
    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert detail.status_code == 200, detail.text
    return detail.json()


class TestCompatibilityCheck:
    async def test_python_agent_passes_real_execution(self, client, user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "passed", body
        assert body["sample_output"] == {"echo": "привет", "length": 6}
        assert body["score"] > 80
        kinds = {item["kind"]: item["status"] for item in body["checks"]}
        assert kinds["input_schema"] == "passed"
        assert kinds["output_schema"] == "passed"
        assert kinds["test_code"] == "passed"
        assert kinds["manifest"] == "passed"

    async def test_wrong_output_schema_fails(self, client, user):
        agent = await python_agent(client, user)
        # Агент возвращает echo/length, а объявлена схема с обязательным summary
        patched = await client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json=version_payload(
                publish=True,
                **{
                    **PYTHON_VERSION,
                    "output_schema": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                    },
                },
            ),
            headers=user.headers,
        )
        assert patched.status_code == 201, patched.text
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        body = response.json()
        assert body["status"] == "failed"
        assert any(item["kind"] == "output_schema" and item["status"] == "failed"
                   for item in body["checks"])

    async def test_negative_check_does_not_affect_readiness(self, client, user):
        """Негативный прогон проверяет валидатор, а не агента: готовность не падает."""
        agent = await python_agent(client, user)
        good = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        assert good.json()["status"] == "passed"
        bad = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility",
            json={"imperfect_output": {"echo": 1}},
            headers=user.headers,
        )
        assert bad.json()["status"] == "failed"
        assert bad.json()["is_negative"] is True
        portal = await client.get("/api/v1/developer/portal", headers=user.headers)
        item = next(
            row for row in portal.json()["agents"] if row["agent_id"] == agent["id"]
        )
        assert item["last_check"]["status"] == "passed"
        assert item["last_negative_check"]["status"] == "failed"

    async def test_negative_test_detects_broken_output(self, client, user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility",
            json={"imperfect_output": {"echo": 42}},
            headers=user.headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "failed"
        failed = {item["kind"] for item in body["checks"] if item["status"] == "failed"}
        assert "output_schema" in failed

    async def test_broken_code_is_reported(self, client, user):
        agent = await python_agent(
            client, user, entrypoint_config={"code": "raise RuntimeError('boom')"}
        )
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        body = response.json()
        assert body["status"] == "failed"
        assert any(item["kind"] == "test_code" and item["status"] == "failed"
                   for item in body["checks"])

    async def test_custom_sample_input(self, client, user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility",
            json={"sample_input": {"input": {"text": "abcd"}}},
            headers=user.headers,
        )
        body = response.json()
        assert body["status"] == "passed"
        assert body["sample_output"] == {"echo": "abcd", "length": 4}

    async def test_sample_input_violating_schema_fails(self, client, user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility",
            json={"sample_input": {"wrong": "shape"}},
            headers=user.headers,
        )
        body = response.json()
        assert body["status"] == "failed"
        assert any(item["kind"] == "input_schema" and item["status"] == "failed"
                   for item in body["checks"])

    async def test_foreign_agent_forbidden(self, client, user, other_user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=other_user.headers
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "forbidden"

    async def test_admin_can_check_any_agent(self, client, user, admin_user):
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=admin_user.headers
        )
        assert response.status_code == 200, response.text

    async def test_agent_without_version(self, client, user):
        agent = await create_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "agent_without_version"

    async def test_unknown_agent(self, client, user):
        response = await client.post(
            "/api/v1/agents/00000000-0000-0000-0000-000000000000/compatibility",
            json={},
            headers=user.headers,
        )
        assert response.status_code == 404

    async def test_history_is_owner_only(self, client, user, other_user):
        agent = await python_agent(client, user)
        await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        mine = await client.get(
            f"/api/v1/agents/{agent['id']}/compatibility", headers=user.headers
        )
        assert mine.status_code == 200
        assert len(mine.json()) == 1
        foreign = await client.get(
            f"/api/v1/agents/{agent['id']}/compatibility", headers=other_user.headers
        )
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "compat_forbidden"

    async def test_http_agent_skips_execution_but_checks_manifest(self, client, user):
        agent = await create_published_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        body = response.json()
        assert response.status_code == 200
        kinds = {item["kind"]: item["status"] for item in body["checks"]}
        assert kinds["test_code"] == "skipped"
        assert body["sample_output"] is None

    async def test_audit_entry_created(self, client, user):
        agent = await python_agent(client, user)
        await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        audit = await client.get(
            "/api/v1/audit", params={"action": "ecosystem.compat_check"}, headers=user.headers
        )
        assert audit.status_code == 200
        assert audit.json()["meta"]["total"] >= 1

    async def test_schema_rejects_limits_above_platform(self, client, user):
        """Первый барьер: схема не даст создать версию с запредельными лимитами."""
        agent = await python_agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json=version_payload(
                publish=True,
                **{
                    **PYTHON_VERSION,
                    "resource_limits": {"max_wall_seconds": 100_000, "max_memory_mb": 32},
                },
            ),
            headers=user.headers,
        )
        assert response.status_code == 422

    async def test_limits_bypassing_schema_are_detected(self, client, user, session):
        """Второй барьер: данные, попавшие в БД в обход схемы, ловит проверка."""
        from sqlalchemy import update

        from app.db.models.agent import AgentVersion

        agent = await python_agent(client, user)
        await session.execute(
            update(AgentVersion)
            .where(AgentVersion.agent_id == uuid.UUID(agent["id"]))
            .values(resource_limits={"max_wall_seconds": 100_000, "max_memory_mb": 32})
        )
        await session.commit()
        check = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        body = check.json()
        limits = next(item for item in body["checks"] if item["kind"] == "limits")
        assert limits["status"] == "failed"
        assert "max_wall_seconds" in limits["message"]
        assert "max_memory_mb" in limits["message"]

    def test_platform_limits_match_agent_schema(self):
        """Два потолка не должны разойтись: сверяем с ResourceLimits."""
        from app.schemas.agent import ResourceLimits
        from app.services.ecosystem import MIN_LIMITS, PLATFORM_LIMITS

        schema = ResourceLimits()
        for field, ceiling in PLATFORM_LIMITS.items():
            metadata = type(schema).model_fields[field].metadata
            limits = [getattr(item, "le", None) for item in metadata]
            assert ceiling in limits, f"{field}: платформа {ceiling}, схема {limits}"
        for field, floor in MIN_LIMITS.items():
            metadata = type(schema).model_fields[field].metadata
            limits = [getattr(item, "ge", None) for item in metadata]
            assert floor in limits, f"{field}: платформа {floor}, схема {limits}"


class TestIntegrations:
    async def test_create_and_list(self, client, user):
        created = await client.post(
            "/api/v1/integrations",
            json={
                "slug": "my-llm",
                "name": "Мой провайдер",
                "summary": "Любой LLM, не привязанный к платформе",
                "kind": "ai_provider",
                "protocol": "openapi",
                "base_url": "https://llm.example.com/v1",
                "docs_url": "https://llm.example.com/docs",
                "capabilities": ["text.generate"],
            },
            headers=user.headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["kind"] == "ai_provider"
        assert body["is_verified"] is False
        listed = await client.get("/api/v1/integrations", headers=user.headers)
        assert listed.status_code == 200
        assert any(item["slug"] == "my-llm" for item in listed.json())

    async def test_slug_unique_per_owner(self, client, user):
        payload = {
            "slug": "dup-integration",
            "name": "Дубль",
            "base_url": "https://example.com/api",
        }
        first = await client.post("/api/v1/integrations", json=payload, headers=user.headers)
        assert first.status_code == 201, first.text
        second = await client.post("/api/v1/integrations", json=payload, headers=user.headers)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "integration_slug_taken"

    async def test_same_slug_other_owner_allowed(self, client, user, other_user):
        payload = {
            "slug": "shared-slug",
            "name": "Свой",
            "base_url": "https://example.com/api",
        }
        assert (
            await client.post("/api/v1/integrations", json=payload, headers=user.headers)
        ).status_code == 201
        other = await client.post("/api/v1/integrations", json=payload, headers=other_user.headers)
        assert other.status_code == 201, other.text

    async def test_invalid_url_scheme(self, client, user):
        response = await client.post(
            "/api/v1/integrations",
            json={"slug": "bad-url", "name": "Плохой", "base_url": "ftp://example.com"},
            headers=user.headers,
        )
        assert response.status_code == 422

    async def test_private_draft_hidden_from_others(self, client, user, other_user):
        created = await client.post(
            "/api/v1/integrations",
            json={
                "slug": "private-one",
                "name": "Приватная",
                "base_url": "https://example.com/api",
                "is_public": False,
            },
            headers=user.headers,
        )
        integration_id = created.json()["id"]
        listing = await client.get("/api/v1/integrations", headers=other_user.headers)
        assert all(item["id"] != integration_id for item in listing.json())
        direct = await client.get(
            f"/api/v1/integrations/{integration_id}", headers=other_user.headers
        )
        assert direct.status_code == 404

    async def test_owner_scoping_on_update_and_delete(self, client, user, other_user):
        created = await client.post(
            "/api/v1/integrations",
            json={"slug": "mine-one", "name": "Моя", "base_url": "https://example.com/api"},
            headers=user.headers,
        )
        integration_id = created.json()["id"]
        denied = await client.patch(
            f"/api/v1/integrations/{integration_id}",
            json={"name": "Взлом"},
            headers=other_user.headers,
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "integration_forbidden"
        deleted = await client.delete(
            f"/api/v1/integrations/{integration_id}", headers=other_user.headers
        )
        assert deleted.status_code == 403
        updated = await client.patch(
            f"/api/v1/integrations/{integration_id}",
            json={"summary": "обновлено"},
            headers=user.headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["summary"] == "обновлено"
        assert (
            await client.delete(
                f"/api/v1/integrations/{integration_id}", headers=user.headers
            )
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/integrations/{integration_id}", headers=user.headers)
        ).status_code == 404

    async def test_kind_filter(self, client, user):
        await client.post(
            "/api/v1/integrations",
            json={
                "slug": "obs-one",
                "name": "Трейсинг",
                "kind": "observability",
                "base_url": "https://trace.example.com",
            },
            headers=user.headers,
        )
        filtered = await client.get(
            "/api/v1/integrations", params={"kind": "observability"}, headers=user.headers
        )
        assert filtered.status_code == 200
        assert all(item["kind"] == "observability" for item in filtered.json())

    async def test_probe_private_address_blocked(self, client, user):
        created = await client.post(
            "/api/v1/integrations",
            json={
                "slug": "internal-probe",
                "name": "Внутренний",
                "base_url": "http://127.0.0.1:8000/api/v1/health",
            },
            headers=user.headers,
        )
        assert created.status_code == 201, created.text
        probe = await client.post(
            f"/api/v1/integrations/{created.json()['id']}/probe", headers=user.headers
        )
        assert probe.status_code == 422
        assert probe.json()["error"]["code"] == "integration_private_address"

    async def test_probe_records_failure(self, client, user, monkeypatch):
        created = await client.post(
            "/api/v1/integrations",
            json={"slug": "unreachable", "name": "Недоступный",
                  "base_url": "https://missing.example.com/api"},
            headers=user.headers,
        )

        async def _boom(*_args, **_kwargs):
            raise httpx.ConnectError("нет соединения")

        monkeypatch.setattr(httpx.AsyncClient, "get", _boom)
        probe = await client.post(
            f"/api/v1/integrations/{created.json()['id']}/probe", headers=user.headers
        )
        assert probe.status_code == 200, probe.text
        body = probe.json()
        assert body["is_verified"] is False
        assert body["failure_count"] == 1
        assert "нет соединения" in body["last_error"]

    async def test_probe_success_marks_verified(self, client, user, monkeypatch):
        created = await client.post(
            "/api/v1/integrations",
            json={"slug": "reachable", "name": "Доступный",
                  "base_url": "https://ok.example.com/api"},
            headers=user.headers,
        )

        class _Response:
            status_code = 200

        async def _ok(*_args, **_kwargs):
            return _Response()

        monkeypatch.setattr(httpx.AsyncClient, "get", _ok)
        probe = await client.post(
            f"/api/v1/integrations/{created.json()['id']}/probe", headers=user.headers
        )
        body = probe.json()
        assert body["is_verified"] is True
        assert body["last_status"] == "200"

    async def test_probe_foreign_forbidden(self, client, user, other_user):
        created = await client.post(
            "/api/v1/integrations",
            json={"slug": "probe-mine", "name": "Моя", "base_url": "https://example.com/api"},
            headers=user.headers,
        )
        response = await client.post(
            f"/api/v1/integrations/{created.json()['id']}/probe", headers=other_user.headers
        )
        assert response.status_code == 403


class TestDeveloperPortal:
    async def test_templates_seeded_and_working(self, client, user):
        response = await client.get("/api/v1/ecosystem/templates", headers=user.headers)
        assert response.status_code == 200, response.text
        items = response.json()
        slugs = {item["slug"] for item in items}
        assert {"python-echo", "python-summarizer", "http-agent", "docker-agent",
                "mcp-server"} <= slugs
        for item in items:
            assert item["files"], item
        assert any(item["runtime"] == "mcp" for item in items)

    async def test_template_detail_increments_downloads(self, client, user):
        first = await client.get("/api/v1/ecosystem/templates/python-echo", headers=user.headers)
        assert first.status_code == 200, first.text
        second = await client.get("/api/v1/ecosystem/templates/python-echo", headers=user.headers)
        assert second.json()["downloads"] == first.json()["downloads"] + 1

    async def test_unknown_template(self, client, user):
        response = await client.get("/api/v1/ecosystem/templates/nope", headers=user.headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "template_not_found"

    async def test_template_code_passes_compatibility(self, client, user):
        """Шаблон, применённый как версия агента, должен проходить ту же проверку."""
        template = (
            await client.get("/api/v1/ecosystem/templates/python-echo", headers=user.headers)
        ).json()
        manifest = template["manifest"]
        agent = await create_agent(client, user)
        version = await client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json=version_payload(
                publish=True,
                entrypoint_type="python",
                entrypoint_config={
                    "code": next(
                        f["content"] for f in template["files"] if f["path"] == "agent.py"
                    ),
                    "sample_input": manifest["sample_input"],
                },
                input_schema=manifest["input_schema"],
                output_schema=manifest["output_schema"],
            ),
            headers=user.headers,
        )
        assert version.status_code == 201, version.text
        check = await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        assert check.status_code == 200, check.text
        assert check.json()["status"] == "passed", check.json()

    async def test_portal_overview(self, client, user):
        agent = await python_agent(client, user)
        await client.post(
            f"/api/v1/agents/{agent['id']}/compatibility", json={}, headers=user.headers
        )
        overview = await client.get("/api/v1/developer/portal", headers=user.headers)
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["agents_total"] >= 1
        assert body["agents_ready"] >= 1
        assert body["templates"] >= 4
        assert any(item["agent_id"] == agent["id"] for item in body["agents"])

    async def test_portal_scoped_to_user(self, client, user, other_user):
        await python_agent(client, user)
        other = await client.get("/api/v1/developer/portal", headers=other_user.headers)
        assert other.json()["agents_total"] == 0

    async def test_ecosystem_stats(self, client, user, admin_user):
        await client.post(
            "/api/v1/integrations",
            json={"slug": "stats-one", "name": "Стат", "base_url": "https://example.com"},
            headers=user.headers,
        )
        stats = await client.get("/api/v1/ecosystem/stats", headers=admin_user.headers)
        assert stats.status_code == 200, stats.text
        body = stats.json()
        assert body["integrations"]["total"] >= 1
        assert body["templates"] >= 4
        assert "agents" in body

    async def test_requires_auth(self, client):
        for path in ("/api/v1/ecosystem/templates", "/api/v1/integrations",
                     "/api/v1/developer/portal"):
            response = await client.get(path)
            assert response.status_code == 401, path
