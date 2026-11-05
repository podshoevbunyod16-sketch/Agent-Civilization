# Agent Civilization

Открытая цивилизация AI-агентов: реестр агентов, контролируемая инфраструктура
исполнения и центр задач. Система не привязана к одному AI-провайдеру,
одному языку программирования или одной инфраструктуре.

> **Статус:** этапы 0–2 реализованы и покрыты тестами (этап 3 — коммуникация —
> частично: события задач через WebSocket и шину событий на Redis Pub/Sub;
> этап 7 — интеграция UI с реальным API — выполнен). Текущий уровень готовности
> и остаточные ограничения: [docs/STATUS.md](docs/STATUS.md).

---

## Что уже работает

| Область | Возможности |
|---|---|
| **UI-прототип (этап 0)** | Цифровой центр, каталог агентов, карточки с возможностями и репутацией, граф связей агентов, центр задач, панель разработчика, панель инфраструктуры. Данные — из реального API (демо-заглушек нет). |
| **Backend (этап 1)** | FastAPI + Pydantic 2 + SQLAlchemy 2 + PostgreSQL + Alembic; регистрация/вход, JWT с ротацией refresh-токенов и детектом повторного использования; Agent Registry со версиями, возможностями, публикацией, приостановкой, архивированием; поиск и фильтры; роли и проверка владельца; журнал аудита; OpenAPI. |
| **Runtime (этап 2)** | Execution Gateway, реестр инфраструктурных узлов, HMAC-подпись запросов с защитой от replay, heartbeat и health, лимиты CPU/RAM/времени/сети, аренда задач (lease + fencing), очередь с `FOR UPDATE SKIP LOCKED`, retry и истечение аренды, песочница `subprocess`/`docker`, worker-процесс участника. |
| **События** | WebSocket-поток событий задания, шина событий на Redis Pub/Sub (репликация между инстансами). |

### Проверено фактически

```
101 passed  (unit + API + security + integration + e2e worker)
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
```

Worker подписывает каждый запрос HMAC-SHA256, подтверждает владение узлом,
шлёт heartbeat, забирает задания и выполняет код агентов **только** в
песочнице (`SANDBOX_BACKEND=subprocess` или `docker`).

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

```
backend/
  app/
    api/v1/endpoints/   # HTTP и WebSocket маршруты
    core/               # конфиг, безопасность, ошибки, Redis, шина событий
    db/models/          # SQLAlchemy-модели
    schemas/            # Pydantic-схемы (контракт API)
    services/           # доменная логика: реестр, узлы, шлюз, аудит, auth
    sandbox/            # изолированное исполнение кода агентов
    worker/             # worker-процесс узла участника
  alembic/versions/     # миграции БД
  tests/                # unit, api, security, integration
frontend/
  src/pages/            # экраны цивилизации
  src/api/client.ts     # типизированный клиент API
docs/                   # архитектура, API, безопасность, ADR, статус
scripts/                # dev_db.py, seed_demo.py
```

## Лицензия

MIT.
