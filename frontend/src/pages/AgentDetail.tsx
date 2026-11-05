import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '@/api/client'
import { Badge, ErrorBox, Loader, Reputation } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { formatDate, label } from '@/lib/format'
import { useAuth } from '@/lib/auth'

export default function AgentDetail() {
  const { id = '' } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const agent = useAsync((signal) => api.agent(id, signal), [id])
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [taskJson, setTaskJson] = useState('{\n  "text": "Проверка агента"\n}')
  const [createdTask, setCreatedTask] = useState<string | null>(null)

  const isOwner = Boolean(user && agent.data && user.id === agent.data.owner.id)
  const canManage = isOwner || user?.role === 'admin'

  async function runAction(action: string, fn: () => Promise<unknown>, message: string) {
    setBusy(action)
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      agent.reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Действие не выполнено')
    } finally {
      setBusy(null)
    }
  }

  async function submitTask() {
    setBusy('task')
    setError(null)
    setNotice(null)
    setCreatedTask(null)
    try {
      const payload = JSON.parse(taskJson) as Record<string, unknown>
      const task = await api.submitTask({ agent_id: id, input_payload: payload, timeout_seconds: 120 })
      setCreatedTask(task.id)
      setNotice(`Задание создано: ${task.status}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Ошибка')
    } finally {
      setBusy(null)
    }
  }

  if (agent.loading) return <Loader rows={5} />
  if (agent.error) return <ErrorBox message={agent.error} onRetry={agent.reload} />
  if (!agent.data) return null

  const data = agent.data
  const currentVersion = data.versions.find((version) => version.version === data.current_version)

  return (
    <>
      <div className="page-head">
        <div>
          <div className="row" style={{ marginBottom: 8 }}>
            <Link to="/agents" className="faint" style={{ fontSize: 12.5 }}>
              ← Каталог
            </Link>
            <Badge value={data.status} />
            {data.is_verified ? <Badge value="ok" text="верифицирован" /> : null}
          </div>
          <h1 className="page-title">{data.name}</h1>
          <div className="agent-slug" style={{ marginTop: 4 }}>
            {data.slug}
          </div>
          <p className="page-sub">{data.summary}</p>
        </div>
        <div className="row">
          <Reputation value={data.reputation_score} />
          {canManage ? (
            <>
              {data.status === 'published' ? (
                <button
                  className="btn btn-sm"
                  disabled={busy !== null}
                  onClick={() =>
                    void runAction('unpublish', () => api.publishAgent(data.id, false), 'Снят с публикации')
                  }
                >
                  Снять с публикации
                </button>
              ) : (
                <button
                  className="btn btn-sm btn-primary"
                  disabled={busy !== null}
                  onClick={() =>
                    void runAction('publish', () => api.publishAgent(data.id, true), 'Опубликован')
                  }
                >
                  Опубликовать
                </button>
              )}
              {data.status === 'suspended' ? (
                <button
                  className="btn btn-sm"
                  disabled={busy !== null}
                  onClick={() => void runAction('resume', () => api.resumeAgent(data.id), 'Работа возобновлена')}
                >
                  Возобновить
                </button>
              ) : (
                <button
                  className="btn btn-sm btn-danger"
                  disabled={busy !== null}
                  onClick={() => {
                    const reason = window.prompt('Причина приостановки?')
                    if (reason) {
                      void runAction('suspend', () => api.suspendAgent(data.id, reason), 'Агент приостановлен')
                    }
                  }}
                >
                  Приостановить
                </button>
              )}
              {data.status === 'draft' ? (
                <button
                  className="btn btn-sm btn-danger"
                  disabled={busy !== null}
                  onClick={() => {
                    if (window.confirm('Удалить черновик агента безвозвратно?')) {
                      void api
                        .deleteAgent(data.id)
                        .then(() => navigate('/agents'))
                        .catch((err: unknown) =>
                          setError(err instanceof ApiError ? err.message : 'Не удалось удалить'),
                        )
                    }
                  }}
                >
                  Удалить
                </button>
              ) : null}
            </>
          ) : null}
        </div>
      </div>

      {notice ? <div className="alert success" style={{ marginBottom: 14 }}>{notice}</div> : null}
      {error ? <div className="alert" style={{ marginBottom: 14 }}>{error}</div> : null}

      <div className="two-col">
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Описание</h2>
            </div>
            <p style={{ color: 'var(--text-dim)', fontSize: 14, whiteSpace: 'pre-wrap', margin: 0 }}>
              {data.description || 'Описание не заполнено.'}
            </p>
            <div className="row" style={{ marginTop: 14 }}>
              {data.tags.map((tag) => (
                <span key={tag} className="tag">
                  #{tag}
                </span>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h2 className="card-title">Возможности</h2>
                <div className="card-sub">
                  Уровень L — заявленный разработчиком; проверенный подтверждается независимо
                </div>
              </div>
            </div>
            <div className="stack" style={{ gap: 8 }}>
              {data.capabilities.map((cap) => (
                <div key={cap.capability_key} className="row between">
                  <div className="row">
                    <span className="mono">{cap.capability_key}</span>
                    {cap.verified_level ? (
                      <span className="badge ok">
                        <span className="dot" /> проверено L{cap.verified_level}
                      </span>
                    ) : (
                      <span className="badge idle">не проверено</span>
                    )}
                  </div>
                  <span className="faint" style={{ fontSize: 12 }}>
                    заявлено L{cap.declared_level}
                  </span>
                </div>
              ))}
              {data.capabilities.length === 0 ? <span className="faint">Нет</span> : null}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2 className="card-title">История версий</h2>
              <span className="card-sub">{data.versions.length} версий</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Версия</th>
                    <th>Тип</th>
                    <th>Изменения</th>
                    <th>Публикация</th>
                  </tr>
                </thead>
                <tbody>
                  {data.versions.map((version) => (
                    <tr key={version.id}>
                      <td className="mono">
                        v{version.version}
                        {version.version === data.current_version ? (
                          <span className="tag" style={{ marginLeft: 8 }}>
                            текущая
                          </span>
                        ) : null}
                      </td>
                      <td>{label(version.entrypoint_type)}</td>
                      <td className="muted" style={{ maxWidth: 320 }}>
                        {version.changelog || '—'}
                      </td>
                      <td className="faint">{formatDate(version.published_at)}</td>
                    </tr>
                  ))}
                  {data.versions.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="empty">
                        Версий пока нет
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>

          {currentVersion ? (
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Контракт вызова (OpenAPI-совместимый)</h2>
                <span className="card-sub">вход / выход</span>
              </div>
              <div className="two-col" style={{ gridTemplateColumns: '1fr 1fr' }}>
                <div>
                  <div className="card-sub" style={{ marginBottom: 6 }}>
                    input_schema
                  </div>
                  <pre className="json">{JSON.stringify(currentVersion.input_schema, null, 2)}</pre>
                </div>
                <div>
                  <div className="card-sub" style={{ marginBottom: 6 }}>
                    output_schema
                  </div>
                  <pre className="json">{JSON.stringify(currentVersion.output_schema, null, 2)}</pre>
                </div>
              </div>
            </div>
          ) : null}
        </div>

        <aside className="stack">
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Карточка</h2>
            </div>
            <dl className="kv">
              <dt>Владелец</dt>
              <dd>{data.owner.display_name}</dd>
              <dt>Категория</dt>
              <dd>{data.category}</dd>
              <dt>Текущая версия</dt>
              <dd className="mono">v{data.current_version || '—'}</dd>
              <dt>Выполнено задач</dt>
              <dd>{data.completed_tasks}</dd>
              <dt>Сбоев</dt>
              <dd>{data.failed_tasks}</dd>
              <dt>Репутация</dt>
              <dd>{data.reputation_score.toFixed(2)} / 100</dd>
              <dt>Опубликован</dt>
              <dd>{formatDate(data.published_at)}</dd>
              <dt>Создан</dt>
              <dd>{formatDate(data.created_at)}</dd>
              {data.homepage_url ? (
                <>
                  <dt>Сайт</dt>
                  <dd>
                    <a href={data.homepage_url} target="_blank" rel="noreferrer">
                      {data.homepage_url}
                    </a>
                  </dd>
                </>
              ) : null}
              {data.repository_url ? (
                <>
                  <dt>Репозиторий</dt>
                  <dd>
                    <a href={data.repository_url} target="_blank" rel="noreferrer">
                      ссылка
                    </a>
                  </dd>
                </>
              ) : null}
            </dl>
          </div>

          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Отправить задание</h2>
            </div>
            {user ? (
              data.status === 'published' ? (
                <div className="stack">
                  <div className="field">
                    <label>input_payload (JSON)</label>
                    <textarea
                      className="textarea"
                      value={taskJson}
                      onChange={(event) => setTaskJson(event.target.value)}
                    />
                  </div>
                  <button className="btn btn-primary" disabled={busy !== null} onClick={() => void submitTask()}>
                    Поставить в очередь
                  </button>
                  {createdTask ? (
                    <Link to={`/tasks/${createdTask}`} className="faint" style={{ fontSize: 12.5 }}>
                      Открыть задание →
                    </Link>
                  ) : null}
                </div>
              ) : (
                <div className="alert info">Задания принимаются только от опубликованных агентов.</div>
              )
            ) : (
              <div className="alert info">
                <Link to="/login">Войдите</Link>, чтобы отправлять задания.
              </div>
            )}
          </div>

          {currentVersion ? (
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Лимиты исполнения</h2>
              </div>
              <dl className="kv">
                {Object.entries(currentVersion.resource_limits).map(([key, value]) => (
                  <div key={key} style={{ display: 'contents' }}>
                    <dt>{key}</dt>
                    <dd className="mono">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ) : null}
        </aside>
      </div>
    </>
  )
}
