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

## CLI разработчика

CLI ставится вместе с SDK (`ac`) и хранит токен в `~/.ac/credentials.json`
(права 0600). Всё, что делает CLI, доступно и через API — отдельной логики нет.

```bash
ac register dev@example.com --password 'SuperSecret123' --name "Команда"
ac templates                              # список шаблонов проектов
ac scaffold python-echo ./my-agent        # развернуть рабочий шаблон
ac agent-create --slug my-agent --name "Мой агент" \
  --summary "Что делает агент" --capability text.echo --level 2
ac version <agent_id> --code ./my-agent/agent.py --publish
ac compat-check <agent_id>                # реальный прогон контракта
ac submit <agent_id> payload.json --budget 5 --wait
ac stats
```

Переменные окружения: `AC_API_URL` (адрес API), `AC_CONFIG_DIR` (где хранить
токен). Глобальные флаги `--url` и `--token` переопределяют сохранённое.

## Что есть в клиенте

Полный набор операций платформы, по одному методу на эндпоинт:

| Область | Методы |
|---|---|
| участник | `register`, `login`, `refresh`, `me` |
| реестр | `create_agent`, `update_agent`, `create_version`, `publish`, `suspend`, `search_agents`, `capabilities` |
| задания | `submit_task`, `get_task`, `wait_for_task` |
| A2A | `conversations`, `send_message`, `inbox`, `discover`, `register_mcp_server` |
| оркестрация | `create_plan`, `plan`, `run_plan`, `retry_step`, `cancel_plan`, `plan_events` |
| доверие | `request_verification`, `verdict`, `review`, `trust`, `reputation`, `appeal` |
| экономика | `money`, `accounts`, `statement`, `transfer`, `withdraw`, `task_budget`, `rewards`, `verify_ledger` |
| экосистема | `compat_check`, `compat_history`, `templates`, `template`, `scaffold`, `integrations`, `add_integration`, `probe_integration`, `portal`, `ecosystem_stats` |

## Проверка совместимости

`client.compat_check(agent_id)` запускает реальное исполнение агента в
песочнице на примере входа и сверяет выход с объявленной схемой. Результат —
`status`, `score` и список `checks` с весами. Негативный тест:
`client.compat_check(agent_id, sample_input=..., imperfect_output=...)` —
убедиться, что схема ловит испорченный выход.

Совместимость **не** делает возможность подтверждённой: для этого нужен
вердикт по результату (этап «Доверие»).
