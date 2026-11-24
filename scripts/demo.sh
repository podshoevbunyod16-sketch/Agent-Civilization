#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
API_URL="${DEMO_API_URL:-http://127.0.0.1:8000}"
UI_URL="${DEMO_UI_URL:-http://127.0.0.1:5173}"
PYTHON="$ROOT/.venv/bin/python"

fail() { echo "[demo] ERROR: $*" >&2; exit 1; }
info() { printf '\n\033[1;36m[demo]\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Не найдена команда '$1'. Установите её и повторите запуск."; }

need node
need npm
need curl
need python3
mkdir -p "$ROOT/var"

USE_DOCKER=false
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  USE_DOCKER=true
fi

if [[ ! -x "$PYTHON" ]]; then
  need python3
  info "Создаю локальное Python-окружение и ставлю зависимости backend"
  python3 -m venv "$ROOT/.venv" || fail "Не удалось создать .venv (проверьте установку python3-venv)."
  "$ROOT/.venv/bin/pip" install -e "$ROOT/backend" || fail "Не удалось установить backend зависимости."
fi
if ! "$PYTHON" -c 'import pgserver' >/dev/null 2>&1; then
  "$ROOT/.venv/bin/pip" install pgserver || fail "Fallback PostgreSQL pgserver не удалось установить."
fi
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  info "Устанавливаю зависимости frontend (npm ci)"
  (cd "$ROOT/frontend" && npm ci) || fail "npm ci завершился с ошибкой."
fi

DATABASE_URL_VALUE="${DATABASE_URL:-}"
if [[ -n "$DATABASE_URL_VALUE" ]]; then
  info "Использую PostgreSQL из DATABASE_URL"
elif [[ "$USE_DOCKER" == true ]]; then
  info "Поднимаю PostgreSQL 16 через Docker Compose"
  docker compose up -d postgres || fail "Не удалось запустить PostgreSQL. Проверьте Docker и порт 5432."
  for attempt in $(seq 1 60); do
    if docker compose exec -T postgres pg_isready -U postgres -d agent_civilization >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 60 ]] || fail "PostgreSQL не стал готов за 60 секунд."
    sleep 1
  done
  DATABASE_URL_VALUE="${DATABASE_URL_VALUE:-postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agent_civilization}"
else
  info "Docker не найден; запускаю PostgreSQL 16 fallback через pgserver"
  if [[ ! -f "$ROOT/var/demo-db.pid" ]] || ! kill -0 "$(cat "$ROOT/var/demo-db.pid")" 2>/dev/null; then
    nohup "$PYTHON" "$ROOT/scripts/dev_db.py" >"$ROOT/var/demo-db.log" 2>&1 & echo $! >"$ROOT/var/demo-db.pid"
  fi
  for attempt in $(seq 1 60); do
    DATABASE_URL_VALUE="$(sed -n 's/^DATABASE_URL=//p' "$ROOT/var/demo-db.log" | tail -1)"
    [[ -n "$DATABASE_URL_VALUE" ]] && break
    [[ "$attempt" -lt 60 ]] || { tail -60 "$ROOT/var/demo-db.log" || true; fail "pgserver не стал готов; проверьте pgserver binaries."; }
    sleep 1
  done
fi
export DATABASE_URL="$DATABASE_URL_VALUE"
export SECRET_KEY="${SECRET_KEY:-local-demo-only-secret-key-change-before-deploy-$(python3 -c 'import secrets; print(secrets.token_hex(8))')}"
export ENVIRONMENT=development
export CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000}"
export SANDBOX_BACKEND="${SANDBOX_BACKEND:-subprocess}"
mkdir -p "$ROOT/var"

info "Применяю миграции Alembic"
(cd "$ROOT/backend" && "$PYTHON" -m alembic upgrade head) || fail "Миграции Alembic не применились."

if ! curl -fsS "$API_URL/api/v1/health" >/dev/null 2>&1; then
  info "Запускаю API на 0.0.0.0:8000"
  (cd "$ROOT/backend" && exec "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000) >"$ROOT/var/demo-api.log" 2>&1 </dev/null &
  echo $! >"$ROOT/var/demo-api.pid"
  for attempt in $(seq 1 60); do
    if curl -fsS "$API_URL/api/v1/health" >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 60 ]] || { tail -60 "$ROOT/var/demo-api.log" || true; fail "API не стал готов за 60 секунд."; }
    sleep 1
  done
else
  info "API уже отвечает на $API_URL"
fi

info "Создаю демо-аккаунт и данные через публичный API"
"$PYTHON" "$ROOT/scripts/seed_demo.py" --api "$API_URL" || fail "Seed сценарий завершился с ошибкой."

if [[ -f "$ROOT/var/seed-node-credentials.json" ]] && { [[ ! -f "$ROOT/var/demo-worker.pid" ]] || ! kill -0 "$(cat "$ROOT/var/demo-worker.pid")" 2>/dev/null; }; then
  info "Запускаю реальный node worker (subprocess sandbox fallback)"
  NODE_KEY="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["node_key"])' "$ROOT/var/seed-node-credentials.json")"
  NODE_SECRET="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["node_secret"])' "$ROOT/var/seed-node-credentials.json")"
  (cd "$ROOT/backend" && AC_API_URL="$API_URL" AC_NODE_KEY="$NODE_KEY" AC_NODE_SECRET="$NODE_SECRET" exec "$PYTHON" -m app.worker.node_worker --poll-interval 0.7 --max-tasks 4) >"$ROOT/var/demo-worker.log" 2>&1 </dev/null &
  echo $! >"$ROOT/var/demo-worker.pid"
fi

if ! curl -fsS "$UI_URL" >/dev/null 2>&1; then
  info "Запускаю Vite на 0.0.0.0:5173"
  (cd "$ROOT/frontend" && exec npm run dev -- --host 0.0.0.0 --port 5173) >"$ROOT/var/demo-ui.log" 2>&1 </dev/null &
  echo $! >"$ROOT/var/demo-ui.pid"
  for attempt in $(seq 1 60); do
    if curl -fsS "$UI_URL" >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 60 ]] || { tail -60 "$ROOT/var/demo-ui.log" || true; fail "Frontend не стал готов за 60 секунд."; }
    sleep 1
  done
fi

info "Демо-стек готов"
printf '  UI:       %s\n  Swagger:  %s/docs\n  MCP:      %s/mcp\n  Логин:    architect@agent-civilization.dev\n  Пароль:   Architect2026 (только локальный seeded demo)\n  Логи:     var/demo-{api,worker,ui}.log\n' "$UI_URL" "$API_URL" "$API_URL"
if [[ "$USE_DOCKER" == true ]]; then
  printf '\nОстановка: kill $(cat var/demo-api.pid var/demo-worker.pid var/demo-ui.pid 2>/dev/null) 2>/dev/null || true ; docker compose stop postgres\n'
else
  printf '\nОстановка: kill $(cat var/demo-api.pid var/demo-worker.pid var/demo-ui.pid var/demo-db.pid 2>/dev/null) 2>/dev/null || true\n'
fi
