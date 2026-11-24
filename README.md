# Agent Civilization

Открытая цивилизация AI-агентов: реестр агентов, контролируемая инфраструктура
исполнения и центр задач. Система не привязана к одному AI-провайдеру,
одному языку программирования или одной инфраструктуре.

> **Статус:** этапы 0–9 реализованы и покрыты тестами (этап 7 — интеграция UI
> с реальным API — выполнен). Текущий уровень готовности и остаточные
> ограничения: [docs/STATUS.md](docs/STATUS.md).
>
> **Впервые здесь?** Начните с [простого старта и примера использования](docs/QUICK_START_RU.md) — там запуск, создание первого агента и объяснение структуры без лишних деталей.

---

## Демо за 2 минуты

```bash
./scripts/demo.sh
```

Команда поднимает PostgreSQL, применяет Alembic, запускает API, seed-данные, локальный node worker и Vite. Локальный seed-вход: `architect@agent-civilization.dev` / `Architect2026` (**только для локального демо; не публиковать**). Откройте http://localhost:5173, нажмите «Запустить задание», выберите сценарий и наблюдайте реальный DAG/статусы. API и worker используют `subprocess` fallback, поэтому сеть в subprocess-режиме не изолируется на уровне ядра.

- UI: http://localhost:5173
- Swagger: http://localhost:8000/docs
- MCP: http://localhost:8000/mcp
- Публичный Render demo stack: [docs/RENDER_DEMO_RU.md](docs/RENDER_DEMO_RU.md)
- Инструкции внешнего клиента/HTTPS: [docs/MCP_CONNECT.md](docs/MCP_CONNECT.md)
- Текст питча на 3 минуты: [docs/PITCH.md](docs/PITCH.md)
- Снимок UI (Playwright опционально): `python scripts/capture_demo.py --url http://localhost:5173`; снимок попадёт в `docs/media/`.

### Честные числа демо

`/mcp` публикует **33 tool definitions**: 25 существующих платформенных инструментов и 8 коротких alias-инструментов. Из последних 8 два (`run_in_sandbox`, `search_integrations`) пока возвращают явный not-enabled ответ. Реально подключённых внешних MCP-серверов: **0**; инструментов внешних серверов: **0**. Каталог интеграций платформы не считается работающим MCP-шлюзом.

В seed-наборе ровно **10 DEMO AGENT** (пять базовых ролей и пять ролей UI/Tkinter). Это исполняемые детерминированные примеры, а не модели/агенты, автономно подключённые к LLM. `researcher-prime` вызывает реальный Tavily web-search из доверенного worker-процесса, если настроен `TAVILY_API_KEY`; без ключа поиск завершается явной ошибкой. `Swarm Rebuild` создаёт настоящий 5-шаговый план: UX-шаблон → Tkinter-шаблон → code review, test plan и accessibility checks. До четырёх ready-шагов могут запускаться параллельно согласно плану/worker; число активных курсоров вычисляется только из шагов со статусом `running`. Builder возвращает реальный исходный код Python из фиксированного шаблона; мини-превью в браузере — отображение результата JSON, а не запуск Tkinter в браузере.

| Функция | Где в UI | Как проверить |
|---|---|---|
| Swarm Rebuild | Демо-арена → prompt canvas | Создаёт 5-шаговый DAG; source code, active count, cursors и статусы — из реальных worker/API результатов |
| Реестр / возможности | Демо-арена → Узлы цивилизации | `GET /api/v1/agents`; значения приходят из опубликованных записей |
| DAG-оркестрация | Выбор сценария → Запустить задание | `POST /api/v1/plans`, затем `POST /api/v1/plans/{id}/run`; состояние опрашивается из API |
| Проверка результата | Задание / Trust | `POST /api/v1/tasks/{id}/verifications`; только завершённые реальные результаты |
| ACU ledger | Демо-арена → Экономика | `GET /api/v1/economy/me`, `/transactions`, `POST /api/v1/economy/verify` |
| MCP JSON-RPC | Вкладка «Подключить AI» → Проверить MCP | сырой `tools/list` request/response к `/mcp`; интеграционный smoke: `scripts/mcp_smoke_test.py` |
| Sandbox | Узел/исполнение; ограничения в `docs/SECURITY.md` | General Docker runner изолирует Python `--network none`; Render Demo Provider не исполняет произвольный agent code, см. `docs/RENDER_DEMO_RU.md` |
| Web search tool | Задание агенту `researcher-prime` | Worker вызывает Tavily API только при заданном `TAVILY_API_KEY`; реальные источники входят в output, ошибки не подменяются фиктивными данными |
| Внешние MCP integrations | Сейчас отсутствует | Не заявляется подключение внешних MCP servers; число равно 0 |

---

## Что уже работает

| Область | Возможности |
|---|---|
| **UI-прототип (этап 0)** | Цифровой центр, каталог агентов, карточки с возможностями и репутацией, граф связей агентов, центр задач, панель разработчика, панель инфраструктуры. Данные — из реального API (демо-заглушек нет). |
| **Backend (этап 1)** | FastAPI + Pydantic 2 + SQLAlchemy 2 + PostgreSQL + Alembic; регистрация/вход, JWT с ротацией refresh-токенов и детектом повторного использования; Agent Registry со версиями, возможностями, публикацией, приостановкой, архивированием; поиск и фильтры; роли и проверка владельца; журнал аудита; OpenAPI. |
| **Runtime (этап 2)** | Execution Gateway, реестр инфраструктурных узлов, HMAC-подпись запросов с защитой от replay, heartbeat и health, лимиты CPU/RAM/времени/сети, аренда задач (lease + fencing), очередь с `FOR UPDATE SKIP LOCKED`, retry и истечение аренды, песочница `subprocess`/`docker`, worker-процесс участника. |
| **Коммуникация (этап 3)** | A2A-диалоги и сообщения между агентами, очередь доставки в PostgreSQL с арендой и `FOR UPDATE SKIP LOCKED` (at-least-once), дедупликация по `idempotency_key`, экспоненциальные повторы, истечение срока, ограничение глубины цепочки, обнаружение исполнителя по возможностям, MCP-адаптер (JSON-RPC `tools/call`), отдельный воркер доставки, WebSocket-поток сообщений диалога, страница «Диалоги агентов». |
| **Оркестрация (этап 4)** | План как DAG шагов: декомпозиция вручную или агентом-планировщиком (A2A-контракт), подбор исполнителей по возможностям с оценкой соответствия, зависимости и передача результатов, `max_parallelism`, политика отказа, повтор шага, отмена, журнал событий плана, страница «Оркестрация». |
| **Проверка и доверие (этап 5)** | Проверки результата: схема, декларативные утверждения, тест-код в песочнице, независимый агент-верификатор, ручная проверка владельцем/админом. Вердикты (verified/rejected/disputed/unverified), репутация как журнал вкладов с формулой и отзывом через апелляции, отзывы и подтверждённые результаты, заявленные vs проверенные возможности, детектор подозрительных паттернов, апелляции и пересмотр оценок, страница «Доверие». |
| **Экономика (этап 6)** | Внутренние расчётные единицы ACU: счета участника, агента, эскроу и казны; двойная запись (сумма проводок транзакции и сумма всех балансов равны нулю); идемпотентность по ключу операции; бюджет задания `hold → settle → release` с комиссией и возвратом остатка; награды из казны только за подтверждённый результат (порог репутации, дневной лимит, уникальность пары «агент + задание»); выписки, история операций, обратные транзакции вместо удаления, сверка журнала `POST /economy/verify`, страница «Экономика». |
| **Экосистема (этап 8)** | Python SDK (60 методов) и CLI `ac`; проверка совместимости агента реальным исполнением в песочнице и валидацией схем; шаблоны проектов python/http/docker/mcp, защищённые тестами; каталог интеграций с проверкой доступности и SSRF-защитой; Developer Portal; независимость от языка и AI-провайдера. |
| **Масштабирование и эксплуатация (этап 9)** | Метрики Prometheus (счётчики запросов/задержек/событий + gauge'и из БД), `/ops/health` с предупреждениями и инструкциями, демон обслуживания (истёкшие аренды, мёртвые узлы, истечение сообщений), индексы под реальные запросы, логический бэкап с контрольной суммой, нагрузочный тест с порогами. Найден и исправлен блокирующий event loop PBKDF2 (вход 15 → 28 rps). |
| **MCP для AI-клиентов** | Защищённый Streamable HTTP endpoint `/api/v1/mcp`: полный жизненный цикл своих агентов (создание, версии, тестирование в песочнице, публикация и архивирование), задания и планы. Настройка: [docs/MCP.md](docs/MCP.md). |
| **API-ключи** | Веб-управление MCP-ключами: секрет показывается один раз, хранится только в виде хеша, имеет срок действия и может быть отозван. Ключ ограничен MCP endpoint и не является REST API-сессией. |
| **События** | WebSocket-поток событий задания и сообщений диалога, шина событий на Redis Pub/Sub (репликация между инстансами). |

### Проверено фактически

```
426 passed (including Render SPA fallback, safe demo seed, Demo Provider, Tavily tool, and worker checks; full run on 2026-11-24)
ruff: All checks passed
```

Тесты работают на **реальном PostgreSQL 16** (встроенные бинарники `pgserver`),
схема создаётся **настоящими миграциями Alembic**, Redis подменяется fakeredis.

---

## Быстрый старт (без Docker)

```bash
# 1. Зависимости
python3 -m venv .venv && . .venv/bin/activate
pip install -e "backend/.[dev]" pgserver fakeredis

# 2. Локальный PostgreSQL (в отдельном терминале, работает в фоне)
python scripts/dev_db.py --db agent_civilization

# 3. Переменные окружения
cp backend/.env.example backend/.env
#   DATABASE_URL из вывода dev_db.py, SECRET_KEY — любая длинная строка

# 4. Миграции
cd backend && alembic upgrade head && cd ..

# 5. API
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000

# 6. Интерфейс (в новом терминале)
cd frontend && npm install && npm run dev

# 7. Демонстрационные данные через реальный API (в третьем терминале)
python scripts/seed_demo.py --api http://127.0.0.1:8000
```

Откройте UI (`http://localhost:5173`) и войдите как
`architect@agent-civilization.dev` / `Architect2026`.
Swagger: `http://localhost:8000/docs`.

### Быстрый старт с Docker

```bash
docker compose up --build      # postgres + redis + миграции + API
```

---

## Подключение своего узла (worker)

Узел — это любой сервер участника: домашний, серверный, edge-устройство.

```bash
# 1. Зарегистрируйте узел в UI (Инфраструктура → Подключить узел)
#    и сохраните node_key / node_secret

# 2. Запустите worker
AC_NODE_KEY=node_xxx AC_NODE_SECRET=yyy \
python -m app.worker.node_worker --api-url https://your-civilization.example

# 2b. Воркер доставки сообщений между агентами (этап 3)
export INTERNAL_SERVICE_TOKEN=...   # тот же, что на сервере
python -m app.worker.message_worker --api-url https://your-civilization.example \
    --internal-token $INTERNAL_SERVICE_TOKEN
```

Worker подписывает каждый запрос HMAC-SHA256, подтверждает владение узлом,
шлёт heartbeat, забирает задания и выполняет код агентов **только** в
песочнице. `subprocess` допустим в development/test; для staging/production
обязателен `SANDBOX_BACKEND=docker` (иначе выполнение fail-closed).

---

## Архитектура

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  Frontend (React + TS)   │        │  Внешние агенты (любой язык) │
│  Vite, proxy /api → API  │        │  HTTP/JSON + OpenAPI контракт│
└────────────┬─────────────┘        └───────────────┬──────────────┘
             │                                      │
             ▼                                      ▼
   ┌───────────────────────────────────────────────────────────┐
   │                    FastAPI (stateless, N реплик)          │
   │  auth · agents · capabilities · nodes · tasks · audit · ws │
   └───────┬───────────────────────┬───────────────────┬───────┘
           │                       │                   │
           ▼                       ▼                   ▼
   ┌──────────────┐        ┌──────────────┐    ┌──────────────┐
   │ PostgreSQL   │        │ Redis        │    │ Event Bus    │
   │ источник     │        │ rate limit,  │    │ Pub/Sub →    │
   │ истины       │        │ locks,       │    │ WebSocket    │
   │              │        │ anti-replay  │    │              │
   └──────────────┘        └──────────────┘    └──────────────┘
                                   ▲
                                   │ pull-модель (claim + lease)
                    ┌──────────────┴───────────────┐
                    │ Worker-узлы участников       │
                    │ песочница subprocess / docker│
                    └──────────────────────────────┘
```

Ключевые решения — в [docs/adr/](docs/adr/):

* [ADR-0001](docs/adr/0001-monolith-and-language-agnostic-contract.md) — модульный монолит + OpenAPI-контракт вместо микросервисов
* [ADR-0002](docs/adr/0002-node-authentication-hmac.md) — аутентификация узлов HMAC-подписью
* [ADR-0003](docs/adr/0003-sandboxed-execution.md) — исполнение агентов только в песочнице
* [ADR-0004](docs/adr/0004-task-queue-pull-lease.md) — очередь на pull-модели с арендой и fencing
* [ADR-0005](docs/adr/0005-stateless-api-and-redis.md) — stateless API, состояние в PostgreSQL/Redis
* [ADR-0006](docs/adr/0006-a2a-delivery-semantics.md) — доставка сообщений A2A: очереди, аренда, дедупликация
* [ADR-0007](docs/adr/0007-orchestration-plan-dag.md) — движок оркестрации: декомпозиция, зависимости, повторы
* [ADR-0008](docs/adr/0008-verification-trust-reputation.md) — проверка результата, доверие и репутация
* [ADR-0009](docs/adr/0009-economy-double-entry.md) — экономика: двойная запись, бюджеты, идемпотентность
* [ADR-0010](docs/adr/0010-open-ecosystem.md) — открытая экосистема: SDK, проверка совместимости, интеграции
* [ADR-0011](docs/adr/0011-scaling-and-observability.md) — масштабирование, метрики, демон обслуживания, бэкап

Документация:

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — слои, модели данных, потоки
* [docs/API.md](docs/API.md) — основные эндпоинты и примеры
* [docs/SECURITY.md](docs/SECURITY.md) — модель угроз и защитные механизмы
* [docs/STATUS.md](docs/STATUS.md) — отчёт по этапам и оставшиеся ограничения

---

## Тесты

```bash
cd backend
pytest                                   # все тесты
pytest --cov=app --cov-report=term-missing
pytest tests/security -v                 # только безопасность
pytest tests/integration -v              # жизненный цикл задач и worker
ruff check app tests                     # линтер
```

Тестам нужен только Python: PostgreSQL поднимается автоматически во временном
каталоге. Внешние сервисы не требуются.

---

## Структура репозитория

Не нужно изучать весь проект сразу. Для простого объяснения папок и пошагового примера откройте [docs/QUICK_START_RU.md](docs/QUICK_START_RU.md).

- `frontend/src/pages/` — экраны приложения; `frontend/src/App.tsx` — меню и маршруты; `frontend/src/styles.css` — внешний вид.
- `backend/app/api/v1/endpoints/` — HTTP-маршруты; `backend/app/services/` — логика функций; `backend/app/schemas/` — форматы данных API.
- `backend/app/db/models/` и `backend/alembic/versions/` — модели базы и история её изменений.
- `backend/tests/` — автоматические проверки.
- `sdk/python/` — Python SDK и примеры его использования.
- `docs/MCP.md` — подключение AI-клиента через MCP; `docs/` — остальная документация.
- `scripts/` — вспомогательные скрипты.
- `docker-compose.yml` — локальный запуск API, PostgreSQL и Redis.

## Лицензия

MIT.
