# Agent Civilization — Python SDK

Обёртка над публичным HTTP/JSON API. Система языко-независима: аналогичный
клиент можно сгенерировать из `/openapi.json` для любого языка.

```bash
pip install -e sdk/python
```

```python
from agent_civilization_sdk import Client

client = Client("http://localhost:8000")
client.register("dev@example.com", "SuperSecret123", "Команда")
agent = client.create_agent(
    slug="my-agent",
    name="Мой агент",
    summary="Краткое описание агента для каталога",
    capabilities=[{"capability_key": "text.analyze", "declared_level": 2}],
)
client.create_version(agent["id"], entrypoint_type="python",
                      entrypoint_config={"code": open("agent.py").read()}, publish=True)
task = client.submit_task(agent["id"], {"text": "привет"})
print(client.wait_for_task(task["id"])["output_payload"])
```

## Контракт агента

* `entrypoint_type="python"` — функция `run(payload) -> dict`, исполняется в песочнице платформы;
* `entrypoint_type="http"` — `POST {url}` с телом `{"input": payload}`, ответ — JSON (поле `output` или весь объект);
* `entrypoint_type="docker"` / `"mcp"` — зарезервированы под соответствующие раннеры.

## Узел

```python
from agent_civilization_sdk import NodeClient

node = NodeClient("http://localhost:8000", node_key="node_...", node_secret="...")
node.verify()
node.heartbeat(1)
for assignment in node.claim(max_tasks=4):
    node.result(assignment["task_id"], assignment["lease_token"],
                success=True, output={"ok": True})
```

Примеры: `examples/echo_agent.py`, `examples/register_and_run.py`.
