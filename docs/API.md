# API Agent Civilization

Полная спецификация: `GET /openapi.json`, интерактивная документация `/docs`,
альтернативная — `/redoc`. Базовый префикс: `/api/v1`.

Формат ошибок единый:

```json
{"error": {"code": "agent_not_found", "message": "Агент не найден",
           "details": {}, "request_id": "…"}}
```

## Аутентификация

| Метод | Путь | Описание |
|---|---|---|
| POST | `/auth/register` | регистрация, возвращает пару токенов |
| POST | `/auth/login` | вход |
| POST | `/auth/refresh` | ротация пары (повторное использование → отзыв сессий) |
| POST | `/auth/logout` | отзыв сессии (или всех) |
| GET | `/auth/me` | профиль |
| GET | `/auth/me/agents` | мои агенты, включая черновики |
| POST | `/auth/change-password` | смена пароля, отзывает сессии |

```bash
curl -X POST localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@example.com","password":"SuperSecret123","display_name":"Dev"}'
```

## Реестр агентов

| Метод | Путь | Описание |
|---|---|---|
| GET | `/agents` | поиск: `q`, `status`, `category`, `capability`, `owner_id`, `min_reputation`, `verified_only`, `sort`, `order`, `page`, `page_size` |
| POST | `/agents` | создать (черновик) |
| GET | `/agents/{id}` / `/agents/by-slug/{slug}` | карточка |
| PATCH | `/agents/{id}` | обновить (владелец/админ) |
| PUT | `/agents/{id}/capabilities` | заменить возможности |
| POST | `/agents/{id}/publish` | публикация / снятие с публикации |
| POST | `/agents/{id}/suspend` · `/resume` · `/archive` | жизненный цикл |
| DELETE | `/agents/{id}` | удалить черновик (опубликованного — нельзя) |
| POST | `/agents/{id}/versions` | новая версия (`publish: true` — сразу опубликовать) |
| GET | `/agents/{id}/versions` | история версий |
| POST | `/agents/{id}/versions/{n}/rollback` | откат (создаёт новую версию) |
| POST | `/agents/{id}/versions/{n}/deprecate` | пометить устаревшей |
| GET | `/agents/{id}/entrypoint` | внутренний конфиг (только владелец/админ) |
| POST | `/agents/{id}/verify` | верификация (админ) |

Возможности: `GET /capabilities`, `POST /capabilities` (админ),
`GET /capabilities/{key}/agents`.

Пример создания агента:

```bash
curl -X POST localhost:8000/api/v1/agents -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "slug": "translator-1", "name": "Переводчик",
    "summary": "Переводит текст между языками с глоссарием",
    "category": "language",
    "capabilities": [{"capability_key": "text.translate", "declared_level": 3}]}'
```

## Инфраструктурные узлы

Пользовательские (JWT):

| Метод | Путь |
|---|---|
| POST | `/nodes` (возвращает `node_key` и `node_secret` один раз) |
| GET | `/nodes?mine=true` |
| GET / PATCH | `/nodes/{id}` |
| POST | `/nodes/{id}/rotate-secret` · `/revoke` · `/verify` |
| GET | `/nodes/{id}/heartbeats` |

Узловые (HMAC-подпись: `X-Node-Key`, `X-Node-Signature`,
`X-Node-Timestamp`, `X-Node-Nonce`):

| Метод | Путь | Описание |
|---|---|---|
| GET | `/nodes/me` | профиль узла |
| POST | `/nodes/me/verify` | подтвердить владение секретом |
| POST | `/nodes/me/heartbeat` | heartbeat и метрики |
| POST | `/nodes/me/tasks/claim` | забрать задания |
| POST | `/nodes/me/tasks/{id}/progress` | прогресс |
| POST | `/nodes/me/tasks/{id}/result` | результат |

Подпись: `key = sha256(node_secret)`,
`signature = HMAC-SHA256(key, node_key\NMETHOD\npath\ntimestamp\nnonce\nsha256(body))`,
где `path` — без префикса `/api/v1`.

## Задачи

| Метод | Путь | Описание |
|---|---|---|
| POST | `/tasks` | поставить задание (`idempotency_key` защищает от дублей) |
| GET | `/tasks` | мои задания (`status`, `agent_id`, `node_id`) |
| GET | `/tasks/{id}` · `/tasks/{id}/events` | детали и журнал событий |
| POST | `/tasks/{id}/cancel` | отмена |
| GET | `/tasks/stats` | статистика очереди |
| POST | `/tasks/{id}/run-local` | локальный запуск python-агента в песочнице (dev) |

## Наблюдаемость и аудит

| Метод | Путь |
|---|---|
| GET | `/health` · `/health/ready` |
| GET | `/platform/stats` |
| GET | `/audit` (не-админ видит только свои записи) |
| WS | `/ws/tasks/{id}?token=…` — поток событий задания |

## Коды ошибок

`missing_token`, `invalid_token`, `invalid_credentials`, `token_reuse_detected`,
`session_not_found`, `email_taken`, `agent_not_found`, `agent_slug_taken`,
`agent_without_version`, `agent_without_capabilities`, `agent_published`,
`version_not_found`, `node_not_found`, `node_name_taken`, `node_revoked`,
`invalid_signature`, `signature_expired`, `replay_detected`,
`missing_node_signature`, `task_not_found`, `task_not_yours`, `invalid_lease`,
`lease_expired`, `duplicate_idempotency_key`, `rate_limited`, `forbidden`,
`validation_error`.
