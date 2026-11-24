"""Protocol-level checks for root Streamable HTTP MCP endpoint."""


async def test_root_mcp_public_handshake_discovery_and_resources(client):
    initialized = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "agent-civilization"

    listing = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listing.status_code == 200
    names = {tool["name"] for tool in listing.json()["result"]["tools"]}
    assert {
        "list_agents",
        "find_agent_for_capability",
        "create_task",
        "get_task_status",
        "get_balance",
        "verify_result",
        "run_in_sandbox",
        "search_integrations",
    } <= names

    resources = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "resources/list"}
    )
    assert resources.json()["result"]["resources"][0]["uri"] == "civilization://about"
    resource = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "civilization://about"},
        },
    )
    assert "not implemented" in resource.json()["result"]["contents"][0]["text"]


async def test_root_mcp_requires_auth_for_user_tools(client):
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "get_balance", "arguments": {}},
        },
    )
    assert response.status_code == 401


async def test_root_mcp_balance_uses_authenticated_ledger(client, user):
    response = await client.post(
        "/mcp",
        headers=user.headers,
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "get_balance", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["balance_units"] >= 0
