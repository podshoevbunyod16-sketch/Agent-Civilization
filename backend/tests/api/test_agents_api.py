"""API-тесты Agent Registry: CRUD, версии, публикация, поиск, права."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import (
    agent_payload,
    create_agent,
    create_published_agent,
    version_payload,
)


# ── Создание и чтение ─────────────────────────────────────────────────
async def test_create_agent_creates_draft_with_capabilities(client, user):
    response = await client.post("/api/v1/agents", json=agent_payload(), headers=user.headers)
    assert response.status_code == 201, response.text
    agent = response.json()
    assert agent["status"] == "draft"
    assert agent["current_version"] == 0
    assert agent["owner"]["id"] == user.id
    assert {c["capability_key"] for c in agent["capabilities"]} == {
        "research.web_search",
        "text.summarize",
    }
    assert all(c["verified_level"] is None for c in agent["capabilities"]), (
        "заявленные возможности не должны автоматически считаться проверенными"
    )


async def test_create_agent_requires_auth(client):
    response = await client.post("/api/v1/agents", json=agent_payload())
    assert response.status_code == 401


async def test_duplicate_slug_conflict(client, user):
    slug = f"dup-{uuid.uuid4().hex[:6]}"
    first = await client.post("/api/v1/agents", json=agent_payload(slug=slug), headers=user.headers)
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/agents", json=agent_payload(slug=slug), headers=user.headers
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "agent_slug_taken"


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "Bad Slug"},
        {"slug": "-leading"},
        {"summary": "too short"},
        {"accent_color": "green"},
        {"tags": ["x" * 40]},
    ],
)
async def test_invalid_agent_payloads_rejected(client, user, overrides):
    response = await client.post(
        "/api/v1/agents", json=agent_payload(**overrides), headers=user.headers
    )
    assert response.status_code == 422, response.text


async def test_duplicate_capability_in_payload_rejected(client, user):
    payload = agent_payload(
        capabilities=[
            {"capability_key": "dup.key", "declared_level": 1},
            {"capability_key": "dup.key", "declared_level": 2},
        ]
    )
    response = await client.post("/api/v1/agents", json=payload, headers=user.headers)
    assert response.status_code == 422


async def test_draft_is_hidden_from_other_users(client, user, other_user):
    agent = await create_agent(client, user)

    owner_view = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert owner_view.status_code == 200

    stranger_view = await client.get(f"/api/v1/agents/{agent['id']}", headers=other_user.headers)
    assert stranger_view.status_code == 404, "чужой черновик не должен быть виден"

    anon_view = await client.get(f"/api/v1/agents/{agent['id']}")
    assert anon_view.status_code == 404


# ── Версии ────────────────────────────────────────────────────────────
async def test_version_creation_increments_and_is_immutable(client, user):
    agent = await create_agent(client, user)
    first = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(),
        headers=user.headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1

    second = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(changelog="v2", entrypoint_config={"url": "https://two.example.com"}),
        headers=user.headers,
    )
    assert second.json()["version"] == 2

    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert detail.json()["current_version"] == 2

    versions = await client.get(f"/api/v1/agents/{agent['id']}/versions", headers=user.headers)
    assert [v["version"] for v in versions.json()] == [2, 1]


async def test_python_entrypoint_requires_code(client, user):
    agent = await create_agent(client, user)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(entrypoint_type="python", entrypoint_config={}),
        headers=user.headers,
    )
    assert response.status_code == 422


async def test_public_view_hides_secrets_from_entrypoint_config(client, user, other_user):
    agent = await create_published_agent(client, user)
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="http",
            entrypoint_config={
                "url": "https://secret.example.com",
                "api_key": "super-secret-key",
                "headers": {"Authorization": "Bearer x"},
            },
        ),
        headers=user.headers,
    )
    public_view = await client.get(f"/api/v1/agents/{agent['id']}", headers=other_user.headers)
    assert public_view.status_code == 200
    serialized = str(public_view.json())
    assert "super-secret-key" not in serialized
    assert "api_key" not in serialized

    owner_view = await client.get(f"/api/v1/agents/{agent['id']}/entrypoint", headers=user.headers)
    assert owner_view.status_code == 200
    assert owner_view.json()["config"]["api_key"] == "super-secret-key"

    stranger_entrypoint = await client.get(
        f"/api/v1/agents/{agent['id']}/entrypoint", headers=other_user.headers
    )
    assert stranger_entrypoint.status_code == 403


async def test_rollback_creates_new_version_with_old_config(client, user):
    agent = await create_agent(client, user)
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(entrypoint_config={"url": "https://v1.example.com"}),
        headers=user.headers,
    )
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(entrypoint_config={"url": "https://v2.example.com"}),
        headers=user.headers,
    )
    rollback = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/1/rollback", headers=user.headers
    )
    assert rollback.status_code == 201, rollback.text
    body = rollback.json()
    assert body["version"] == 3
    assert body["entrypoint_config"]["url"] == "https://v1.example.com"


# ── Публикация ────────────────────────────────────────────────────────
async def test_cannot_publish_without_version(client, user):
    agent = await create_agent(client, user)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/publish", json={"publish": True}, headers=user.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_without_version"


async def test_cannot_publish_without_capabilities(client, user):
    agent = await create_agent(client, user, capabilities=[])
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions", json=version_payload(), headers=user.headers
    )
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/publish", json={"publish": True}, headers=user.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_without_capabilities"


async def test_cannot_publish_version_without_capabilities(client, user):
    agent = await create_agent(client, user, capabilities=[])
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(publish=True),
        headers=user.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_without_capabilities"


async def test_publish_makes_agent_visible_in_catalog(client, user, other_user):
    agent = await create_published_agent(client, user)
    catalog = await client.get("/api/v1/agents")
    ids = {item["id"] for item in catalog.json()["items"]}
    assert agent["id"] in ids


async def test_unpublish_and_suspend_lifecycle(client, user):
    agent = await create_published_agent(client, user)
    agent_id = agent["id"]

    unpublish = await client.post(
        f"/api/v1/agents/{agent_id}/publish", json={"publish": False}, headers=user.headers
    )
    assert unpublish.status_code == 200
    assert unpublish.json()["status"] == "draft"

    suspend = await client.post(
        f"/api/v1/agents/{agent_id}/suspend",
        json={"reason": "нарушение правил"},
        headers=user.headers,
    )
    assert suspend.status_code == 200
    assert suspend.json()["status"] == "suspended"

    anonym = await client.get(f"/api/v1/agents/{agent_id}")
    assert anonym.status_code == 200, "приостановленный агент виден с явным статусом"
    assert anonym.json()["status"] == "suspended"

    resume = await client.post(f"/api/v1/agents/{agent_id}/resume", headers=user.headers)
    assert resume.status_code == 200
    assert resume.json()["status"] == "published"


async def test_published_agent_cannot_be_deleted(client, user):
    agent = await create_published_agent(client, user)
    response = await client.delete(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_published"


async def test_draft_can_be_deleted(client, user):
    agent = await create_agent(client, user)
    response = await client.delete(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert response.status_code == 204
    assert (
        await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    ).status_code == 404


# ── Права владельца ───────────────────────────────────────────────────
async def test_only_owner_can_update_agent(client, user, other_user):
    agent = await create_agent(client, user)
    forbidden = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={"name": "Взломанный агент"},
        headers=other_user.headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    owner_update = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={"name": "Новое имя агента"},
        headers=user.headers,
    )
    assert owner_update.status_code == 200
    assert owner_update.json()["name"] == "Новое имя агента"


async def test_admin_can_update_any_agent(client, user, admin_user):
    agent = await create_agent(client, user)
    response = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={"category": "reviewed"},
        headers=admin_user.headers,
    )
    assert response.status_code == 200


async def test_capabilities_replacement_resets_verification(client, user):
    agent = await create_published_agent(client, user)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/capabilities",
        json={"capabilities": [{"capability_key": "code.write", "declared_level": 4}]},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    keys = [c["capability_key"] for c in response.json()["capabilities"]]
    assert keys == ["code.write"]

    registry = await client.get("/api/v1/capabilities")
    assert "code.write" in {c["key"] for c in registry.json()}, (
        "новая возможность регистрируется в таксономии"
    )


# ── Поиск и фильтры ───────────────────────────────────────────────────
async def test_search_filters_by_text_category_and_capability(client, user):
    await create_published_agent(
        client, user, name="Агент-переводчик", category="language", tags=["translate"]
    )
    target = await create_published_agent(
        client, user, name="Агент-аналитик данных", category="data"
    )

    by_text = await client.get("/api/v1/agents", params={"q": "аналитик"})
    assert [i["id"] for i in by_text.json()["items"]] == [target["id"]]

    by_category = await client.get("/api/v1/agents", params={"category": "data"})
    assert target["id"] in {i["id"] for i in by_category.json()["items"]}

    by_capability = await client.get("/api/v1/agents", params={"capability": "research.web_search"})
    assert target["id"] in {i["id"] for i in by_capability.json()["items"]}

    no_match = await client.get("/api/v1/agents", params={"q": "не-существует-такого"})
    assert no_match.json()["items"] == []
    assert no_match.json()["meta"]["total"] == 0


async def test_pagination_metadata(client, user):
    for _ in range(5):
        await create_published_agent(client, user, category="paging")
    response = await client.get(
        "/api/v1/agents", params={"category": "paging", "page": 2, "page_size": 2}
    )
    body = response.json()
    assert len(body["items"]) == 2
    assert body["meta"] == {"page": 2, "page_size": 2, "total": 5, "pages": 3}


async def test_sort_by_name_ascending(client, user):
    await create_published_agent(client, user, name="Бета агент", category="sorting")
    await create_published_agent(client, user, name="Альфа агент", category="sorting")
    response = await client.get(
        "/api/v1/agents",
        params={"category": "sorting", "sort": "name", "order": "asc"},
    )
    names = [i["name"] for i in response.json()["items"]]
    assert names == sorted(names)


async def test_page_size_limit_enforced(client):
    response = await client.get("/api/v1/agents", params={"page_size": 5000})
    assert response.status_code == 422


async def test_draft_with_versions_can_be_deleted(client, user):
    """Регресс: удаление агента с версиями падало на NOT NULL agent_versions.agent_id."""
    agent = await create_agent(client, user)
    created_version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(),
        headers=user.headers,
    )
    assert created_version.status_code == 201

    response = await client.delete(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert response.status_code == 204, response.text
    assert (
        await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    ).status_code == 404
    versions = await client.get(f"/api/v1/agents/{agent['id']}/versions", headers=user.headers)
    assert versions.status_code == 404


async def test_agent_with_task_history_cannot_be_deleted(client, user):
    """История заданий — часть аудита: агент архивируется, а не удаляется."""
    agent = await create_published_agent(client, user)
    task = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {"q": "x"}},
        headers=user.headers,
    )
    assert task.status_code == 201, task.text

    await client.post(
        f"/api/v1/agents/{agent['id']}/publish", json={"publish": False}, headers=user.headers
    )
    response = await client.delete(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "agent_has_tasks"
    assert response.json()["error"]["details"]["tasks"] == 1
