"""Tests for the authenticated MCP HTTP server."""

from tests.conftest import create_published_agent


async def test_mcp_requires_authentication(client):
    response = await client.post(
        "/api/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


async def test_mcp_initialize_and_list_tools(client, user):
    initialized = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "agent-civilization"

    listed = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} == {
        "find_agents",
        "run_agent",
        "check_task",
        "start_workflow",
        "check_workflow",
    }


async def test_mcp_can_find_and_run_published_agent(client, user):
    agent = await create_published_agent(client, user)
    found = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_agents", "arguments": {"query": agent["name"]}},
        },
    )
    assert found.status_code == 200
    matches = found.json()["result"]["structuredContent"]["agents"]
    assert any(item["id"] == agent["id"] for item in matches)

    started = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run_agent",
                "arguments": {"agent_id": agent["id"], "input": {"hello": "world"}},
            },
        },
    )
    assert started.status_code == 200
    task_id = started.json()["result"]["structuredContent"]["task_id"]

    checked = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "check_task", "arguments": {"task_id": task_id}},
        },
    )
    assert checked.status_code == 200
    assert checked.json()["result"]["structuredContent"]["id"] == task_id


async def test_mcp_workflow_tools_create_and_read_plan(client, user):
    agent = await create_published_agent(client, user)
    created = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": "workflow",
            "method": "tools/call",
            "params": {
                "name": "start_workflow",
                "arguments": {
                    "title": "Обработать текст",
                    "goal": "Сделать краткую сводку",
                    "steps": [
                        {"name": "Обработать", "agent_id": agent["id"], "input": {"text": "пример"}}
                    ],
                },
            },
        },
    )
    assert created.status_code == 200, created.text
    plan_id = created.json()["result"]["structuredContent"]["plan_id"]

    checked = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": "check",
            "method": "tools/call",
            "params": {"name": "check_workflow", "arguments": {"plan_id": plan_id}},
        },
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["result"]["structuredContent"]["plan_id"] == plan_id


async def test_mcp_accepts_revocable_api_key(client, user):
    created = await client.post(
        "/api/v1/auth/api-keys",
        headers=user.headers,
        json={"name": "MCP test"},
    )
    assert created.status_code == 201, created.text
    api_key = created.json()["api_key"]

    response = await client.post(
        "/api/v1/mcp",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["result"]["tools"]) == 5
