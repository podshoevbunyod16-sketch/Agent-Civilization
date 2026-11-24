# Публичное демо на Render

Этот Blueprint создаёт **один API+SPA web service** и PostgreSQL. UI и API работают
на одном origin; миграции запускаются перед деплоем. После первоначального deploy,
регистрации demo-пользователя и создания node добавьте отдельный Render Background
Worker. Он запускает только встроенный allow-list `Demo Provider`: никакой код из
реестра и никакие shell-команды не исполняются. Реальные snippets web search идут
через отдельный Tavily tool.

## Перед созданием

- Если старый Render project содержит нужные данные, сначала экспортируйте базу.
  Удаление проекта/базы может удалить их без возможности восстановления.
- Слияние конфигурации в `main` обязательно перед выбором Blueprint из GitHub.
- Managed PostgreSQL на Render и web service могут быть платными; проверьте план
  и оценку стоимости в Render перед подтверждением Blueprint.

## Создать стек с нуля

1. В Render выберите **New → Blueprint**, подключите репозиторий Agent Civilization
   и ветку `main`. Blueprint прочитает `render.yaml`.
2. Проверьте, что создаются только `agent-civilization-1` и
   `agent-civilization-db`. Подтвердите создание. API и собранный React UI
   находятся в одном Docker image.
3. Дождитесь успешного deploy и миграций. Проверки:

   ```text
   https://agent-civilization-1.onrender.com/api/v1/health
   https://agent-civilization-1.onrender.com/
   ```

   Первая должна вернуть health JSON, вторая — открыть React-приложение.
   Если назначенный Render hostname отличается, измените `CORS_ORIGINS` у web
   service на фактический HTTPS origin и redeploy.

## Создать аккаунт и опубликовать demo-агентов

1. Зарегистрируйте отдельный аккаунт через публичный UI и войдите в него.
   Он не получает admin-роли автоматически.
2. На своём компьютере в корне checkout установите только backend runtime deps:

   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -e backend
   ```

   Windows PowerShell: используйте `.venv\Scripts\python.exe` и `.venv\Scripts\pip.exe`.
3. Запустите безопасный idempotent seed. Скрипт попросит пароль скрытым prompt;
   пароль не передавайте аргументом, не отправляйте в чат и не сохраняйте в Git:

   ```bash
   .venv/bin/python scripts/seed_render_demo.py \
     --api https://agent-civilization-1.onrender.com \
     --email YOUR_REGISTERED_EMAIL
   ```

   Скрипт публикует десять карточек `DEMO AGENT` только от вошедшего пользователя;
   он не создаёт demo-пользователя, не повышает роли, не создаёт узел и не печатает
   токены. Шаблонные агенты не являются LLM.

## Подключить Render Background Worker и tool web_search

Worker добавляется **после** создания demo-агентов и node: его HMAC credentials
показываются только при регистрации node.

1. В UI откройте **Инфраструктура → добавить узел**. Capabilities задайте ровно:
   `demo.provider`, `research.web_search`, `python.syntax_check`; лимиты — не менее
   1 CPU, 512 MB RAM, 60 секунд wall time. Сохраните `node_key` и `node_secret` в
   password manager: secret показывается один раз. Не помещайте их в Git или чат.
2. В Render создайте **New → Background Worker** из того же репозитория и ветки
   `main`. Выберите Docker runtime, Dockerfile `backend/Dockerfile`, context `.` и
   Start Command `python -m app.worker.demo_node_worker`.
3. В Environment worker-сервиса добавьте:

   ```text
   ENVIRONMENT=production
   AC_API_URL=https://agent-civilization-1.onrender.com
   AC_NODE_KEY=...            # one-time value from node creation
   AC_NODE_SECRET=...         # one-time value from node creation
   TAVILY_API_KEY=...         # если нужен реальный web_search
   ```

   Секреты добавляются только через Render Environment. Worker не требует
   `DATABASE_URL` и Docker daemon: он использует тот же image, но запускает только
   фиксированный список demo handlers, не исполняет код из реестра и shell.
   Проверяйте Render Worker logs: должны появиться успешный verify и heartbeat.

Если нужно запустить тот же demo worker на своём компьютере вместо Render Background
Worker, установите backend runtime deps, заполните `.env.example` локально и
выполните `scripts/run_render_demo_worker.sh`.

`researcher-prime` обращается к Tavily только при наличии `TAVILY_API_KEY`, только
для задания владельца seed-агента и не более 12 раз в час на worker process; результат
содержит фактические provider snippets.
При отсутствии ключа или ошибке провайдера задача завершится явной ошибкой, не
выдуманными результатами. Другие demo-агенты используют детерминированные handlers.
Код Tkinter compile-проверяется как текст, но не запускается и не исполняется в GUI.

## Что увидит инвестор / честные ограничения

- Каталог, план, назначенный узел, статусы, события и task outputs — из реальной
  Render PostgreSQL/API/worker; реально выполненные инструменты перечислены в
  `tool_calls` результата и в task lifecycle events.
- Demo provider — детерминированные seed-агенты, **не настоящая LLM**. Поиск —
  реальные snippets от Tavily, если worker настроен с ключом; сам Tkinter builder
  выдаёт Python-шаблон.
- Произвольный Shell/CLI не публикуется. Render Demo Worker не исполняет агентский
  Python: он создаёт код как результат и проверяет синтаксис через compile без
  исполнения; web search выполняет доверенный worker tool.
- Если Render Background Worker остановлен, задания остаются в очереди, а UI показывает
  фактический статус. Для непрерывного демо worker должен работать постоянно.
- Этот Render Blueprint не добавляет внешнего LLM provider и не делает Claude Code,
  Codex или ChatGPT CLI доступными инструментами.
