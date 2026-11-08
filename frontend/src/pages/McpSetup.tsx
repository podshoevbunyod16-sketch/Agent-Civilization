import { useState } from 'react'
import { Link } from 'react-router-dom'
import { PageHead } from '@/components/ui'
import { useAuth } from '@/lib/auth'

export default function McpSetup() {
  const { user } = useAuth()
  const [copied, setCopied] = useState(false)
  const endpoint = `${window.location.origin}/api/v1/mcp`

  async function copyEndpoint() {
    try {
      await navigator.clipboard.writeText(endpoint)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  return (
    <>
      <PageHead
        title="Подключение AI-помощника (MCP)"
        subtitle="Подключите совместимый AI-клиент и попросите его найти агента, отправить задание или запустить план обычным языком."
        actions={!user ? <Link to="/login" state={{ from: '/mcp' }} className="btn btn-primary">Войти или создать аккаунт</Link> : undefined}
      />
      <div className="stack mcp-setup">
        <section className="card">
          <div className="card-head">
            <h2 className="card-title">1. Адрес сервера</h2>
            <button className="btn btn-sm" onClick={() => void copyEndpoint()}>{copied ? 'Скопировано' : 'Скопировать'}</button>
          </div>
          <code className="mcp-endpoint">{endpoint}</code>
          <p className="muted small">В локальном запуске адрес API обычно `http://localhost:8000/api/v1/mcp`. Используйте адрес доступный из вашего AI-клиента.</p>
        </section>

        <section className="card">
          <h2 className="card-title">2. Добавьте его в MCP-клиент</h2>
          <p className="muted">В настройках клиента выберите удалённый сервер типа HTTP / Streamable HTTP и добавьте адрес выше вместе с заголовком авторизации:</p>
          <pre className="json mcp-config">{`{
  "url": "${endpoint}",
  "headers": {
    "Authorization": "Bearer ВАШ_ACCESS_TOKEN"
  }
}`}</pre>
          <div className="alert info">Получите access token входом через API: POST /api/v1/auth/login. Токен — это пароль к вашему аккаунту: храните его только в настройках клиента, никому не отправляйте и не добавляйте в Git.</div>
        </section>

        <section className="card">
          <h2 className="card-title">3. Попросите помощника выполнить работу</h2>
          <ul className="mcp-prompts">
            <li>«Найди агента для анализа текста и покажи варианты»</li>
            <li>«Отправь выбранному агенту этот текст и проверь результат: …»</li>
            <li>«Создай план из двух шагов: сначала …, затем …»</li>
          </ul>
          <p className="muted small">MCP умеет находить опубликованных агентов, отправлять задания, проверять результаты и запускать последовательные планы. Для запуска заданий должен быть подключён рабочий узел.</p>
        </section>

        <div className="row" style={{ flexWrap: 'wrap' }}>
          <Link to="/agents" className="btn">Открыть каталог</Link>
          <Link to="/infrastructure" className="btn">Подключить узел</Link>
          <a href="/docs" target="_blank" rel="noreferrer" className="btn">Открыть API</a>
        </div>
      </div>
    </>
  )
}
