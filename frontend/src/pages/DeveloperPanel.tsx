import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { formatDate } from '@/lib/format'
import { useAuth } from '@/lib/auth'

const PYTHON_TEMPLATE = `def run(payload):
    """Точка входа агента. payload — входные данные задания."""
    text = payload.get("text", "")
    return {"length": len(text), "echo": text}`

const HTTP_TEMPLATE = '{\n  "url": "https://my-agent.example.com/a2a",\n  "api_key": "optional-bearer-token"\n}'

type Mode = 'python' | 'http'

export default function DeveloperPanel() {
  const { user } = useAuth()
  const [tab, setTab] = useState<'create' | 'mine' | 'audit'>('create')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [createdId, setCreatedId] = useState<string | null>(null)

  const [form, setForm] = useState({
    name: '',
    slug: '',
    summary: '',
    description: '',
    category: 'general',
    tags: '',
    capabilities: 'text.analyze',
  })

  const [version, setVersion] = useState({
    mode: 'python' as Mode,
    changelog: 'Первая версия',
    code: PYTHON_TEMPLATE,
    httpConfig: HTTP_TEMPLATE,
    publish: true,
    maxWallSeconds: 30,
    maxMemoryMb: 256,
    allowNetwork: false,
  })

  const myAgents = useAsync(
    (signal) => (user ? api.myAgents(signal) : Promise.resolve([])),
    [user?.id, tab],
  )
  const audit = useAsync(
    () => (user ? fetchAuditLog() : Promise.resolve(null)),
    [user?.id, tab],
  )

  async function fetchAuditLog() {
    const response = await fetch('/api/v1/audit?page_size=25', {
      headers: { Authorization: `Bearer ${localStorage.getItem('ac.access_token') ?? ''}` },
    })
    if (!response.ok) throw new Error('Не удалось загрузить журнал аудита')
    return (await response.json()) as {
      items: Array<{
        id: string
        action: string
        target_type: string
        target_id: string | null
        outcome: string
        created_at: string
      }>
    }
  }

  function autoSlug(name: string) {
    return name
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .trim()
      .replace(/\s+/g, '-')
      .slice(0, 60)
  }

  async function createAgent() {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const slug = form.slug || autoSlug(form.name) || `agent-${Date.now()}`
      const agent = await api.createAgent({
        name: form.name,
        slug,
        summary: form.summary,
        description: form.description,
        category: form.category,
        tags: form.tags
          .split(',')
          .map((tag) => tag.trim())
          .filter(Boolean),
        capabilities: form.capabilities
          .split(',')
          .map((key) => key.trim())
          .filter(Boolean)
          .map((key) => ({ capability_key: key, declared_level: 2 })),
      })

      const entrypoint =
        version.mode === 'python'
          ? { type: 'python', config: { code: version.code, runtime: 'python3.12' } }
          : { type: 'http', config: JSON.parse(version.httpConfig) as Record<string, unknown> }

      await api.createVersion(agent.id, {
        changelog: version.changelog,
        entrypoint_type: entrypoint.type,
        entrypoint_config: entrypoint.config,
        input_schema: { type: 'object', properties: { text: { type: 'string' } } },
        output_schema: { type: 'object' },
        resource_limits: {
          max_cpu_seconds: Math.min(60, version.maxWallSeconds),
          max_wall_seconds: version.maxWallSeconds,
          max_memory_mb: version.maxMemoryMb,
          allow_network: version.allowNetwork,
          max_output_bytes: 65536,
        },
        publish: version.publish,
      })

      setCreatedId(agent.id)
      setNotice(`Агент «${agent.name}» создан${version.publish ? ' и опубликован' : ' как черновик'}`)
      setForm({ ...form, name: '', slug: '', summary: '', description: '', tags: '' })
      myAgents.reload()
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.message}${err.code ? ` (${err.code})` : ''}`
          : err instanceof Error
            ? err.message
            : 'Не удалось создать агента',
      )
    } finally {
      setBusy(false)
    }
  }

  if (!user) {
    return (
      <>
        <PageHead
          title="Панель разработчика"
          subtitle="Регистрация агентов, версии, публикация и журнал аудита."
        />
        <div className="alert info">
          Войдите или зарегистрируйтесь, чтобы создавать агентов. Аккаунт разработчика создаётся моментально.
        </div>
        <Link to="/login" state={{ from: '/developer/agents' }} className="btn btn-primary">
          Войти или создать аккаунт
        </Link>
      </>
    )
  }

  return (
    <>
      <PageHead
        title="Панель разработчика"
        subtitle="Создайте агента, опишите возможности и опубликуйте его. Система не привязана к языку или AI-провайдеру: контракт — HTTP/JSON."
        actions={
          <div className="row">
            <button className={`btn btn-sm${tab === 'create' ? ' btn-primary' : ''}`} onClick={() => setTab('create')}>
              Новый агент
            </button>
            <button className={`btn btn-sm${tab === 'mine' ? ' btn-primary' : ''}`} onClick={() => setTab('mine')}>
              Мои агенты
            </button>
            <button className={`btn btn-sm${tab === 'audit' ? ' btn-primary' : ''}`} onClick={() => setTab('audit')}>
              Аудит
            </button>
          </div>
        }
      />

      {notice ? <div className="alert success" style={{ marginBottom: 14 }}>{notice}</div> : null}
      {error ? <div className="alert" style={{ marginBottom: 14 }}>{error}</div> : null}

      {tab === 'create' ? (
        <div className="two-col">
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Карточка агента</h2>
              <span className="card-sub">шаг 1 из 2</span>
            </div>
            <div className="form-grid">
              <div className="field">
                <label>Имя *</label>
                <input
                  className="input"
                  value={form.name}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      name: event.target.value,
                      slug: form.slug || autoSlug(event.target.value),
                    })
                  }
                  placeholder="Агент-переводчик"
                />
              </div>
              <div className="field">
                <label>Slug (уникальный)</label>
                <input
                  className="input"
                  value={form.slug}
                  onChange={(event) => setForm({ ...form, slug: autoSlug(event.target.value) })}
                  placeholder="agent-translator"
                />
              </div>
              <div className="field">
                <label>Категория</label>
                <input
                  className="input"
                  value={form.category}
                  onChange={(event) => setForm({ ...form, category: event.target.value })}
                />
              </div>
              <div className="field">
                <label>Теги (через запятую)</label>
                <input
                  className="input"
                  value={form.tags}
                  onChange={(event) => setForm({ ...form, tags: event.target.value })}
                  placeholder="translate, language"
                />
              </div>
            </div>
            <div className="field" style={{ marginTop: 14 }}>
              <label>Краткое описание * (10–280 символов)</label>
              <input
                className="input"
                value={form.summary}
                onChange={(event) => setForm({ ...form, summary: event.target.value })}
                placeholder="Переводит текст между языками с сохранением терминологии"
              />
            </div>
            <div className="field" style={{ marginTop: 14 }}>
              <label>Полное описание</label>
              <textarea
                className="textarea"
                style={{ fontFamily: 'inherit', fontSize: 13.5 }}
                value={form.description}
                onChange={(event) => setForm({ ...form, description: event.target.value })}
              />
            </div>
            <div className="field" style={{ marginTop: 14 }}>
              <label>Возможности (ключи через запятую)</label>
              <input
                className="input"
                value={form.capabilities}
                onChange={(event) => setForm({ ...form, capabilities: event.target.value })}
              />
              <span className="faint" style={{ fontSize: 11.5 }}>
                Новые ключи автоматически регистрируются в таксономии как заявленные возможности.
              </span>
            </div>
          </div>

          <div className="stack">
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Версия и точка входа</h2>
                <span className="card-sub">шаг 2 из 2</span>
              </div>
              <div className="row" style={{ marginBottom: 12 }}>
                <button
                  className={`btn btn-sm${version.mode === 'python' ? ' btn-primary' : ''}`}
                  onClick={() => setVersion({ ...version, mode: 'python' })}
                >
                  Python (sandbox)
                </button>
                <button
                  className={`btn btn-sm${version.mode === 'http' ? ' btn-primary' : ''}`}
                  onClick={() => setVersion({ ...version, mode: 'http' })}
                >
                  HTTP endpoint
                </button>
              </div>
              {version.mode === 'python' ? (
                <div className="field">
                  <label>Код агента (функция run(payload) → dict)</label>
                  <textarea
                    className="textarea"
                    style={{ minHeight: 170 }}
                    value={version.code}
                    onChange={(event) => setVersion({ ...version, code: event.target.value })}
                  />
                  <span className="faint" style={{ fontSize: 11.5 }}>
                    Код исполняется в изолированной песочнице с лимитами CPU/RAM/времени.
                    Сеть по умолчанию запрещена.
                  </span>
                </div>
              ) : (
                <div className="field">
                  <label>Конфигурация endpoint (JSON)</label>
                  <textarea
                    className="textarea"
                    style={{ minHeight: 120 }}
                    value={version.httpConfig}
                    onChange={(event) => setVersion({ ...version, httpConfig: event.target.value })}
                  />
                  <span className="faint" style={{ fontSize: 11.5 }}>
                    api_key не показывается другим участникам (скрывается в публичном API).
                  </span>
                </div>
              )}
              <div className="form-grid" style={{ marginTop: 12 }}>
                <div className="field">
                  <label>Лимит времени, сек</label>
                  <input
                    className="input"
                    type="number"
                    value={version.maxWallSeconds}
                    onChange={(event) =>
                      setVersion({ ...version, maxWallSeconds: Number(event.target.value) })
                    }
                  />
                </div>
                <div className="field">
                  <label>Лимит памяти, МБ</label>
                  <input
                    className="input"
                    type="number"
                    value={version.maxMemoryMb}
                    onChange={(event) => setVersion({ ...version, maxMemoryMb: Number(event.target.value) })}
                  />
                </div>
              </div>
              <div className="row between" style={{ marginTop: 14 }}>
                <label className="row" style={{ fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={version.publish}
                    onChange={(event) => setVersion({ ...version, publish: event.target.checked })}
                  />
                  Опубликовать сразу
                </label>
                <button
                  className="btn btn-primary"
                  disabled={busy || form.name.length < 2 || form.summary.length < 10}
                  onClick={() => void createAgent()}
                >
                  {busy ? 'Создание…' : 'Создать агента'}
                </button>
              </div>
              {createdId ? (
                <div style={{ marginTop: 12 }}>
                  <Link to={`/agents/${createdId}`} className="btn btn-sm">
                    Открыть карточку →
                  </Link>
                </div>
              ) : null}
            </div>

            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Документация</h2>
              </div>
              <div className="stack" style={{ fontSize: 13 }}>
                <a href="/docs" target="_blank" rel="noreferrer" className="btn btn-sm">
                  OpenAPI / Swagger UI
                </a>
                <a href="/redoc" target="_blank" rel="noreferrer" className="btn btn-sm">
                  ReDoc
                </a>
                <span className="faint" style={{ fontSize: 12 }}>
                  Любой язык, поддерживающий HTTP/JSON, может зарегистрировать агента. SDK: Python-пакет
                  в каталоге <code>sdk/python</code>.
                </span>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {tab === 'mine' ? (
        myAgents.loading ? (
          <Loader rows={4} />
        ) : myAgents.error ? (
          <ErrorBox message={myAgents.error} onRetry={myAgents.reload} />
        ) : (myAgents.data ?? []).length === 0 ? (
          <Empty>
            <p>У вас пока нет агентов.</p>
            <button className="btn btn-primary btn-sm" onClick={() => setTab('create')}>
              Создать первого
            </button>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Агент</th>
                  <th>Статус</th>
                  <th>Версия</th>
                  <th>Возможности</th>
                  <th>Задачи</th>
                  <th>Создан</th>
                </tr>
              </thead>
              <tbody>
                {(myAgents.data ?? []).map((agent) => (
                  <tr key={agent.id}>
                    <td>
                      <Link to={`/agents/${agent.id}`} style={{ fontWeight: 600 }}>
                        {agent.name}
                      </Link>
                      <div className="faint mono" style={{ fontSize: 11 }}>
                        {agent.slug}
                      </div>
                    </td>
                    <td>
                      <Badge value={agent.status} />
                    </td>
                    <td className="mono">v{agent.current_version || '—'}</td>
                    <td className="faint" style={{ fontSize: 12 }}>
                      {agent.capabilities.map((cap) => cap.capability_key).join(', ') || '—'}
                    </td>
                    <td>
                      {agent.completed_tasks}/{agent.failed_tasks}
                    </td>
                    <td className="faint">{formatDate(agent.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : null}

      {tab === 'audit' ? (
        audit.loading ? (
          <Loader rows={5} />
        ) : audit.error ? (
          <ErrorBox message={audit.error} onRetry={audit.reload} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Время</th>
                  <th>Действие</th>
                  <th>Объект</th>
                  <th>Результат</th>
                </tr>
              </thead>
              <tbody>
                {(audit.data?.items ?? []).map((entry) => (
                  <tr key={entry.id}>
                    <td className="faint mono" style={{ fontSize: 11.5 }}>
                      {formatDate(entry.created_at)}
                    </td>
                    <td className="mono">{entry.action}</td>
                    <td className="faint">
                      {entry.target_type}
                      {entry.target_id ? ` · ${entry.target_id.slice(0, 8)}…` : ''}
                    </td>
                    <td>
                      <Badge value={entry.outcome === 'success' ? 'ok' : 'bad'} text={entry.outcome} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : null}
    </>
  )
}
