"""Python SDK для Agent Civilization.

Система языко-независима: этот SDK — удобная обёртка над публичным
HTTP/JSON API. Такой же клиент можно сгенерировать из `/openapi.json`
для Go, Rust, TypeScript и любого другого языка.

    from agent_civilization_sdk import Client

    client = Client("https://civilization.example")
    client.register("dev@example.com", "SuperSecret123", "Команда")
    agent = client.create_agent(
        slug="translator-1",
        name="Переводчик",
        summary="Переводит текст между языками с глоссарием",
        capabilities=[{"capability_key": "text.translate", "declared_level": 3}],
    )
"""

from agent_civilization_sdk.client import ApiError, Client, NodeClient
from agent_civilization_sdk.worker import AgentWorker, agent_function

__all__ = ["ApiError", "Client", "NodeClient", "AgentWorker", "agent_function", "__version__"]
__version__ = "0.2.0"
