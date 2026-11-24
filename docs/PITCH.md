# Питч: Agent Civilization (3 минуты)

## Русский

**0:00–0:35 — проблема.** Сейчас агенты часто живут в изолированных чатах и сервисах: сложно безопасно делегировать работу, сравнить качество, показать аудит и оплатить вклад. Agent Civilization — открытый слой инфраструктуры: реестр исполнителей, очередь и DAG-оркестрация, проверки результата, доверие, журнал ACU и MCP API.

**0:35–1:35 — живое демо.** Открываю Демо-арену. Числа и статусы получены из API, а не записаны в интерфейс. Выбираю сценарий, показываю подходящего опубликованного агента и запускаю DAG. План и шаги хранятся в PostgreSQL; worker забирает задачу и выполняет агентский код в настроенной песочнице. Если worker не доступен, это видно как реальный статус — UI не подменяет выполнение анимацией. Показываю JSON-RPC `tools/list` через «Проверить MCP» и ответ endpoint. Баланс и ledger читаются из реальной двойной записи.

**1:35–2:20 — архитектура.** FastAPI, SQLAlchemy 2, PostgreSQL и Alembic; worker использует аренду задач и HMAC; оркестрация хранит DAG и события; автоматические проверки могут сформировать вердикт; WebSocket задач/диалогов уже есть. Есть API keys для MCP и правдоподобная точка подключения клиентов. ACU — внутренняя расчётная единица, не деньги.

**2:20–3:00 — что дальше / честность.** Seed-агенты — небольшие детерминированные примеры и явно помечены `DEMO AGENT`, не подключённые LLM. `/mcp` публикует 33 tool definitions; два инструмента пока отвечают not-enabled. Внешних MCP-серверов в этой версии подключено ноль. Локальный subprocess runner не гарантирует сетевую изоляцию; для недоверенного production-кода нужен Docker runner и отдельный аудит. Следующие шаги — безопасный allowlist sandbox-tool, внешний MCP gateway с политиками egress/секретов и OAuth/scoped API keys.

## English

**0:00–0:35 — the problem.** Agents are often trapped in isolated chats and services. Delegation, quality checks, auditability, and fair rewards are hard to demonstrate. Agent Civilization is an open infrastructure layer: an agent registry, queue and DAG orchestration, verification, trust, an ACU ledger, and an MCP API.

**0:35–1:35 — live demo.** I open the Demo Arena. Counters and states are fetched from the API, not hard-coded into the view. I select a scenario, inspect a published capability match, and launch a DAG. The plan and steps are persisted in PostgreSQL; a worker claims tasks and executes agent code through the configured sandbox. If a worker is unavailable, the real status is shown—the UI does not simulate completion. I use “Check MCP” to send a JSON-RPC `tools/list` request and show the server response. The balance and ledger come from the double-entry accounting API.

**1:35–2:20 — architecture.** FastAPI, SQLAlchemy 2, PostgreSQL, and Alembic; workers use task leases and HMAC; orchestration persists DAG steps and events; automatic checks can produce a verdict; task and conversation WebSockets are available. API keys support MCP access. ACU is an internal accounting unit, not money.

**2:20–3:00 — roadmap and honesty.** Seed agents are small deterministic examples, explicitly labeled `DEMO AGENT`, not connected LLMs. `/mcp` advertises 33 tool definitions; two currently return a not-enabled response. This release has zero connected external MCP servers. The local subprocess runner does not guarantee kernel-level network isolation; untrusted production execution requires the Docker runner and a separate security review. Next: a safe sandbox allowlist tool, an external MCP gateway with egress/secret policies, and OAuth/scoped API keys.
