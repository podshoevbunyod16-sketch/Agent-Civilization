"""Tests for the authenticated MCP HTTP server."""

from tests.conftest import create_published_agent

MCP_ECHO_CODE = """def run(payload):
    text = payload.get("input", {}).get("text", "")
    return {"echo": text, "length": len(text)}
"""


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
    tool_names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {
        "find_agents",
        "run_agent",
        "check_task",
        "start_workflow",
        "check_workflow",
        "list_my_agents",
        "create_agent",
        "create_agent_version",
        "test_agent",
        "publish_agent",
    } <= tool_names


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

    listed = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": "list-workflows",
            "method": "tools/call",
            "params": {"name": "list_my_workflows", "arguments": {}},
        },
    )
    assert any(
        item["plan_id"] == plan_id
        for item in listed.json()["result"]["structuredContent"]["workflows"]
    )

    cancelled = await client.post(
        "/api/v1/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": "cancel-workflow",
            "method": "tools/call",
            "params": {
                "name": "cancel_workflow",
                "arguments": {"plan_id": plan_id, "reason": "тест отмены"},
            },
        },
    )
    assert cancelled.json()["result"]["structuredContent"]["status"] == "cancelled"


async def test_mcp_agent_lifecycle_create_test_publish_and_run(client, user, other_user):
    async def call_tool(name, arguments, *, headers=None):
        return await client.post(
            "/api/v1/mcp",
            headers=headers or user.headers,
            json={
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )

    created = await call_tool(
        "create_agent",
        {
            "slug": "mcp-managed-echo",
            "name": "MCP Echo Agent",
            "summary": "Echo text and report its length from a sandbox.",
            "capabilities": [{"capability_key": "text.echo", "declared_level": 2}],
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["result"]["structuredContent"]
    assert agent["status"] == "draft"
    agent_id = agent["id"]

    version = await call_tool(
        "create_agent_version",
        {
            "agent_id": agent_id,
            "entrypoint_type": "python",
            "entrypoint_config": {
                "code": MCP_ECHO_CODE,
                "sample_input": {"input": {"text": "hello"}},
            },
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
        },
    )
    assert version.status_code == 200, version.text
    version_data = version.json()["result"]["structuredContent"]
    assert version_data["version"] == 1
    assert "entrypoint_config" not in version_data

    second_version = await call_tool(
        "create_agent_version",
        {
            "agent_id": agent_id,
            "entrypoint_type": "python",
            "entrypoint_config": {"code": MCP_ECHO_CODE},
        },
    )
    assert second_version.json()["result"]["structuredContent"]["version"] == 2
    rolled_back = await call_tool(
        "rollback_agent_version", {"agent_id": agent_id, "version": 1}
    )
    rollback_data = rolled_back.json()["result"]["structuredContent"]
    assert rollback_data["version"] == 3
    assert "entrypoint_config" not in rollback_data
    versions = await call_tool("list_agent_versions", {"agent_id": agent_id})
    assert len(versions.json()["result"]["structuredContent"]["items"]) == 3

    tested = await call_tool(
        "test_agent",
        {"agent_id": agent_id, "sample_input": {"input": {"text": "hello"}}},
    )
    assert tested.status_code == 200, tested.text
    test_data = tested.json()["result"]["structuredContent"]
    assert test_data["status"] == "passed", test_data
    assert test_data["sample_output"] == {"echo": "hello", "length": 5}

    forbidden = await call_tool(
        "publish_agent",
        {"agent_id": agent_id, "publish": True},
        headers=other_user.headers,
    )
    assert forbidden.status_code == 200
    assert forbidden.json()["result"]["isError"] is True

    published = await call_tool("publish_agent", {"agent_id": agent_id, "publish": True})
    assert published.status_code == 200, published.text
    assert published.json()["result"]["structuredContent"]["status"] == "published"
    blocked_version = await call_tool(
        "create_agent_version",
        {
            "agent_id": agent_id,
            "entrypoint_type": "python",
            "entrypoint_config": {"code": MCP_ECHO_CODE},
        },
    )
    assert blocked_version.json()["result"]["isError"] is True

    mine = await call_tool("list_my_agents", {})
    assert any(
        item["id"] == agent_id for item in mine.json()["result"]["structuredContent"]["agents"]
    )

    public = await call_tool("find_agents", {"query": "MCP Echo Agent"})
    assert any(
        item["id"] == agent_id for item in public.json()["result"]["structuredContent"]["agents"]
    )

    started = await call_tool(
        "run_agent", {"agent_id": agent_id, "input": {"input": {"text": "run"}}}
    )
    assert started.status_code == 200, started.text
    task_id = started.json()["result"]["structuredContent"]["task_id"]
    checked = await call_tool("check_task", {"task_id": task_id})
    assert checked.json()["result"]["structuredContent"]["id"] == task_id

    listed_tasks = await call_tool("list_my_tasks", {})
    assert any(
        task["id"] == task_id
        for task in listed_tasks.json()["result"]["structuredContent"]["tasks"]
    )
    cancelled = await call_tool("cancel_task", {"task_id": task_id, "reason": "тест отмены"})
    assert cancelled.json()["result"]["structuredContent"]["status"] == "cancelled"

    updated = await call_tool(
        "update_agent",
        {
            "agent_id": agent_id,
            "name": "MCP Echo Agent Updated",
            "change_note": "Обновлено через MCP",
        },
    )
    assert updated.json()["result"]["structuredContent"]["name"] == "MCP Echo Agent Updated"

    capabilities = await call_tool(
        "replace_agent_capabilities",
        {
            "agent_id": agent_id,
            "capabilities": [{"capability_key": "text.echo", "declared_level": 3}],
        },
    )
    assert capabilities.json()["result"]["structuredContent"]["capabilities"][0][
        "declared_level"
    ] == 3

    suspended = await call_tool("suspend_agent", {"agent_id": agent_id, "reason": "Проверка"})
    assert suspended.json()["result"]["structuredContent"]["status"] == "suspended"
    resumed = await call_tool("resume_agent", {"agent_id": agent_id})
    assert resumed.json()["result"]["structuredContent"]["status"] == "published"
    unpublished = await call_tool("publish_agent", {"agent_id": agent_id, "publish": False})
    assert unpublished.json()["result"]["structuredContent"]["status"] == "draft"
    archived = await call_tool("archive_agent", {"agent_id": agent_id})
    assert archived.json()["result"]["structuredContent"]["status"] == "archived"
    archived_delete = await call_tool("delete_draft_agent", {"agent_id": agent_id})
    assert archived_delete.json()["result"]["isError"] is True

    disposable = await call_tool(
        "create_agent",
        {
            "slug": "mcp-disposable",
            "name": "Disposable MCP Agent",
            "summary": "A disposable draft agent for the deletion test.",
        },
    )
    disposable_id = disposable.json()["result"]["structuredContent"]["id"]
    deleted = await call_tool("delete_draft_agent", {"agent_id": disposable_id})
    assert deleted.json()["result"]["structuredContent"]["deleted"] is True


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
    assert "create_agent" in {tool["name"] for tool in response.json()["result"]["tools"]}

    called = await client.post(
        "/api/v1/mcp",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_my_agents", "arguments": {}},
        },
    )
    assert called.status_code == 200, called.text
    assert "agents" in called.json()["result"]["structuredContent"]
