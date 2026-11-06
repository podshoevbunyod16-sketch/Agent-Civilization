# Production checklist

Каждый пункт отвечает на вопрос «проверено ли это». Статусы:

* **✅ проверено** — есть автоматический тест или замер, ссылка на него;
* **⚙️ реализовано** — код есть, но в этой среде проверить нельзя
  (нужна инфраструктура: TLS-терминатор, managed Postgres, KMS);
* **❌ не сделано** — честно отсутствует.

Правило проекта: «production-ready» не пишется, пока не закрыты все ✅ и
пункты с пометкой «блокирует».

## 1. Конфигурация

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 1.1 | Отказ старта при небезопасной конфигурации | ✅ | `tests/api/test_security_fixes.py::test_unsafe_production_config_refuses_to_start`, шаг CI «Security tests» |
| 1.2 | `SECRET_KEY` ≥ 32 символов, не `change-me` | ✅ | там же (`SECRET_KEY`) |
| 1.3 | `DEBUG=false` обязателен | ✅ | там же (`DEBUG`) |
| 1.4 | CORS: только https, без `*` | ✅ | там же (`CORS_ORIGINS`) |
| 1.5 | `INTERNAL_SERVICE_TOKEN` обязателен | ✅ | там же |
| 1.6 | `SECRET_ENCRYPTION_KEYS` обязателен (не вывод из `SECRET_KEY`) | ✅ | там же |
| 1.7 | `PUBLIC_BASE_URL` — https (в письмах одноразовые токены) | ✅ | там же |
| 1.8 | `SANDBOX_BACKEND=docker`, сеть запрещена | ✅ | там же (`SANDBOX_*`) |
| 1.9 | `SSRF_ALLOW_PRIVATE=false` | ✅ | там же |
| 1.10 | `.env.example` содержит все переменные с пояснениями | ✅ | `.env.example`, сверка `test_env_example_covers_settings` |

## 2. Аутентификация и доступ

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 2.1 | Выход отзывает access-токен немедленно | ✅ | `test_auth_security.py::test_access_token_bound_to_session` |
| 2.2 | Reuse refresh-токена → отзыв всех сессий | ✅ | `test_refresh_reuse_detection_revokes_all` |
| 2.3 | Блокировка перебора по аккаунту (не только по IP) | ✅ | `test_bruteforce_lockout_by_account`, `test_forwarded_for_cannot_bypass_bruteforce_lockout` |
| 2.4 | 2FA (TOTP) + одноразовые коды восстановления | ✅ | `test_totp_full_flow`, `test_totp_secret_encrypted_at_rest` |
| 2.5 | Подтверждение email, сброс пароля: TTL, single-use, хеш в БД | ✅ | `test_email_token_is_single_use`, `test_password_reset_flow_revokes_sessions` |
| 2.6 | API-ключи: хеш, срок, отзыв, только свой | ✅ | `test_api_key_lifecycle`, `test_api_key_cannot_revoke_foreign` |
| 2.7 | Admin-действия пишутся в аудит | ✅ | `tests/api/test_ops_api.py`, таблица `audit_log` |

## 3. Секреты

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 3.1 | Секреты в БД только в AES-256-GCM | ✅ | `tests/unit/test_secrets.py::test_mcp_token_encrypted_in_db` |
| 3.2 | Версионирование ключей и ротация | ✅ | `test_rotation_reads_old_and_writes_new`, `test_unknown_version_has_explicit_error` |
| 3.3 | Подмена шифротекста обнаруживается | ✅ | `test_tampered_ciphertext_rejected` |
| 3.4 | Секреты не попадают в логи (в т.ч. `extra`) | ✅ | `test_json_logger_filters_secrets`, `test_scrub_removes_secrets_from_structures` |
| 3.5 | Внешний Secret Manager (Vault/KMS) | ❌ | интерфейс `SecretProvider` есть, реализация — задача инфраструктуры |

## 4. Сеть и изоляция

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 4.1 | SSRF: резолв DNS + блокировка внутренних диапазонов | ✅ | `tests/unit/test_net_guard.py` (37 кейсов) |
| 4.2 | Подключение к проверенному IP (DNS-rebinding) | ✅ | `test_dns_rebinding_connects_to_checked_ip` |
| 4.3 | Редиректы не следуются автоматически | ✅ | `SafeAsyncClient(follow_redirects=False)` |
| 4.4 | WebSocket: тикет вместо токена в query | ✅ | `test_ws_ticket_is_short_lived_and_one_time`, `test_ws_rejects_access_token_in_query` |
| 4.5 | Лимит соединений на пользователя | ✅ | `WS_MAX_CONNECTIONS_PER_USER`, `/ws/info` |
| 4.6 | TLS-терминация/HSTS за прокси | ⚙️ | HSTS выставляется при `ENVIRONMENT=production`, сертификаты — у балансировщика |
| 4.7 | Security headers (CSP, nosniff, frame-ancestors) | ✅ | `test_security_headers_present` |

## 5. Надёжность

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 5.1 | Захват задания без дублей (`SKIP LOCKED`) | ✅ | `tests/api/test_orchestration_api.py`, `test_outbox.py::test_concurrent_claim_does_not_duplicate` |
| 5.2 | Fencing аренды (`lease_token`) | ✅ | `tests/api/test_gateway_*`, `invalid_lease` |
| 5.3 | Возврат истёкших аренд | ✅ | `maintenance.run_once`, `/ops/health` (`stuck_leases`) |
| 5.4 | События не теряются и не «фантомят» (outbox) | ✅ | `tests/api/test_outbox.py` (8 тестов) |
| 5.5 | Retry с backoff и dead-letter | ✅ | `test_failed_publish_is_retried_with_backoff`, `test_dead_letter_after_max_attempts` |
| 5.6 | Застрявшие отправки возвращаются | ✅ | `test_stuck_sending_rows_are_reclaimed` |
| 5.7 | Гарантия доставки заявлена как at-least-once | ✅ | `docs/adr/0013-outbox-at-least-once.md` |
| 5.8 | Redis недоступен → лимиты не отключаются | ✅ | `test_rate_limit_uses_local_fallback_when_redis_down` |
| 5.9 | Redis недоступен → anti-replay fail-closed | ✅ | `test_anti_replay_fails_closed_without_redis` |

## 6. Финансы

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 6.1 | Двойная запись держится на любых суммах | ✅ | `test_financial_invariants.py` (Hypothesis, 25 примеров) |
| 6.2 | Сумма балансов = 0 | ✅ | `_assert_invariants` |
| 6.3 | Кэш баланса = сумма проводок | ✅ | там же |
| 6.4 | Нет lost update при конкурентных переводах | ✅ | `test_concurrent_transfers_do_not_lose_money` |
| 6.5 | Идемпотентность операций | ✅ | `test_idempotency_key_prevents_double_spend` |
| 6.6 | Ограничения в БД (не только в коде) | ✅ | `test_db_rejects_unbalanced_ledger_directly`, миграция `a1f4c9d2e7b3` |
| 6.7 | Только Decimal на границе, 4 знака | ✅ | `test_four_decimal_places_are_exact`, `test_five_decimal_places_rejected` |
| 6.8 | Корректировки только обратными проводками | ✅ | `test_reversal_restores_balances_and_keeps_history` |

## 7. Бэкапы и DR

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 7.1 | Бэкап шифруется (AES-256-GCM) | ✅ | `test_backup_creates_encrypted_file_with_manifest` |
| 7.2 | Целостность проверяется (checksum + манифест) | ✅ | `test_tampered_backup_rejected` |
| 7.3 | **Восстановление реально выполняется** | ✅ | `test_backup_restore_verifies_rows`, `test_backup_worker_passes_and_verifies` |
| 7.4 | Retention не оставляет «сирот» | ✅ | `test_retention_removes_old_backups` |
| 7.5 | PITR/WAL-архив | ⚙️ | `docs/runbooks/backup-restore.md` §5 — требование к кластеру |
| 7.6 | Отдельное хранилище бэкапов | ⚙️ | монтирование тома, вне репозитория |
| 7.7 | RPO/RTO заявлены и обоснованы | ✅ | runbook §6 (цель + что измерено) |

## 8. Наблюдаемость

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 8.1 | Метрики HTTP, домена, очередей | ✅ | `/metrics`, `tests/api/test_ops_api.py` |
| 8.2 | Correlation ID в каждой записи лога | ✅ | `test_log_context_adds_correlation_id` |
| 8.3 | Логи без секретов | ✅ | `test_json_logger_filters_secrets` |
| 8.4 | Health/readiness/liveness | ✅ | `/health`, `/health/ready`, `/api/v1/ops/readiness` |
| 8.5 | Алерты (правила) | ⚙️ | метрики и пороги есть; доставка алертов — у платформы |
| 8.6 | Outbox dead-letter виден | ✅ | `outbox_dead_letter`, gauge `outbox_failed` |
| 8.7 | Задержка заданий (p50/p95/p99) | ✅ | `task_duration_ms`, `task_execution_ms`, `tests/api/test_observability_metrics.py` |
| 8.8 | Отказы MCP по типу | ✅ | `mcp_error{outcome}`, `test_mcp_errors_are_counted_by_type` |
| 8.9 | Пул соединений БД | ✅ | `db_pool_checked_out/overflow/utilization`, `test_pool_snapshot_of_queue_pool` |
| 8.10 | Живость воркеров (не только БД) | ✅ | `worker_*_age_seconds`, `worker_stalled`, `test_worker_becomes_stale_after_threshold` |
| 8.11 | Фактическая конфигурация доступна для сверки | ✅ | `/ops/health` → `config` (без секретов) |

## 9. CI и безопасность поставки

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 9.1 | SAST блокирует HIGH | ✅ | CI job `security`, локально: `bandit -r app -ll -ii` → 0 HIGH |
| 9.2 | Аудит зависимостей | ✅ | `pip-audit` → «No known vulnerabilities found» |
| 9.3 | Скан секретов в репозитории | ✅ | job `security`, шаг «Secret scan» |
| 9.4 | Скан контейнера (CRITICAL блокирует) | ✅ | `trivy-action`, `severity: CRITICAL`, `ignore-unfixed` |
| 9.5 | Security-тесты в CI отдельным шагом | ✅ | шаг «Security tests» |

## 9a. Нагрузка и chaos (этап H)

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 9a.1 | Пропускная способность и p50/p95/p99 измерены | ✅ | `docs/runbooks/load-and-chaos.md` §2: read 85.6 rps (p95 188 мс), mixed 76.3 rps (p95 203 мс), auth 21.5 rps (p95 583 мс) |
| 9a.2 | Параметры замера сохранены | ✅ | JSON-отчёт `bench_report.py --out` + `config` из `/ops/health` в том же отчёте |
| 9a.3 | CPU/RAM/пул/очередь фиксируются | ✅ | `/proc/<pid>/stat` + `/metrics`; RSS 131.6 MB, CPU 74–152 % ядра |
| 9a.4 | Chaos: Redis недоступен | ✅ | `redis-down`: 0 ответов 5xx, подписи узлов fail-closed |
| 9a.5 | Chaos: Redis мигает под нагрузкой | ✅ | `redis-flap`: 20 параллельных чтений, 0 ответов 5xx |
| 9a.6 | Chaos: воркер умер, аренда вернулась | ✅ | `worker-killed`: аренда взята, истекла через 10.1 с, `requeued_tasks=1` |
| 9a.7 | Chaos: исчерпание пула БД | ✅ | `db-pool-exhausted`: 40 параллельных, 40×200, 0 ответов 5xx |
| 9a.8 | Найденные дефекты исправлены и закрыты тестами | ✅ | `tests/api/test_overdue_tasks.py` (5 тестов): залипание очереди и голодание выдачи |
| 9a.9 | Замер на production-подобной инфраструктуре | ❌ | нет балансировщика, реального Redis и внешнего драйвера нагрузки — см. runbook §6 |

## 10. Что осознанно НЕ сделано

* Полное PITR внутри приложения — это задача кластера, а не кода.
* Внешний Secret Manager — интерфейс готов, сервис не подключён.
* Автоматическая доставка алертов (PagerDuty/Opsgenie) — вне репозитория.
* Production-подобный нагрузочный стенд: балансировщик, реальный Redis,
  внешний драйвер нагрузки, ≥ 2 реплики API — измерено то, что измерено
  (`docs/runbooks/load-and-chaos.md`).
* Exactly-once доставка событий — не заявляется, см. ADR 0013.
* Нагрузочные цифры на «миллионах пользователей» — не заявляются:
  измерено то, что измерено (2 vCPU, локальный Postgres) в
  `docs/STATUS.md`.
