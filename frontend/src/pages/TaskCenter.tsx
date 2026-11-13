import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, api, type TaskEvent } from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead } from '@/components/ui'
import { useAsync, usePolling } from '@/lib/useAsync'
import { formatDate, relativeTime, truncate } from '@/lib/format'
import { useAuth } from '@/lib/auth'

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'queued', label: 'В очереди' },
  { value: 'running', label: 'В работе' },
  { value: 'succeeded', label: 'Выполнено' },
  { value: 'failed', label: 'Ошибки' },
  { value: 'cancelled', label: 'Отменено' },
]

export default function TaskCenter() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [status, setStatus] = useState('')
  const [selectedAgent, setSelectedAgent] = useState('')
  const [inputText, setInputText] = useState('Привет!')
  const [inputJson, setInputJson] = useState('{\n  "text": "Привет!"\n}')
  const [advancedInput, setAdvancedInput] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const publishedAgents = useAsync(
    (signal) => api.agents({ status: 'published', page_size: 100, sort: 'reputation' }, signal),
    [],
  )
  const [page, setPage] = useState(1)

  const tasks = useAsync(
    (signal) =>
      api.tasks(
        { status: status || undefined, page, page_size: 15 },
        signal,
      ),
    [status, page, user?.id],
  )

  const detail = useAsync(
    (signal) => (id ? api.task(id, signal) : Promise.resolve(null)),
    [id],
  )

  const [events, setEvents] = useState<TaskEvent[]>([])
  const [live, setLive] = useState(false)

  // Живые события через WebSocket; при недоступности — периодический опрос
  useEffect(() => {
    if (!id) {
      setEvents([])
      return
    }
    let closed = false
    let socket: WebSocket | null = null
    setEvents([])
    setLive(false)

    const connect = async () => {
      let ticket: string | null = null
      try {
        ticket = (await api.wsTicket()).ticket
      } catch {
        setLive(false)
        return
      }
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${window.location.host}/api/v1/ws/tasks/${id}?ticket=${encodeURIComponent(ticket)}`
      try {
        socket = new WebSocket(url)
        socket.onopen = () => setLive(true)
        socket.onmessage = (event) => {
          const message = JSON.parse(event.data as string) as {
            type: string
            events?: TaskEvent[]
            event_type?: string
            actor?: string
            message?: string
            data?: Record<string, unknown>
            created_at?: string
          }
          if (message.type === 'snapshot') {
            setEvents(message.events ?? [])
          } else if (message.type === 'event') {
            setEvents((previous) => [
              ...previous,
              {
                id: `${message.event_type}-${previous.length}`,
                sequence: previous.length + 1,
                event_type: message.event_type ?? 'event',
                actor: message.actor ?? 'system',
                message: message.message ?? '',
                data: message.data ?? {},
                created_at: message.created_at ?? new Date().toISOString(),
              },
            ])
            detail.reload()
            tasks.reload()
          }
        }
        socket.onerror = () => setLive(false)
        socket.onclose = () => {
          if (!closed) setLive(false)
        }
      } catch {
        setLive(false)
      }
    }
    void connect()

    return () => {
      closed = true
      socket?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  usePolling(() => {
    if (id && !live) {
      detail.reload()
      if (events.length === 0) {
        void api.taskEvents(id).then(setEvents).catch(() => undefined)
      }
    }
  }, 4000, Boolean(id))

  usePolling(() => tasks.reload(), 10000, !id)

  async function cancelTask(taskId: string) {
    const reason = window.prompt('Причина отмены?', 'передумал')
    if (reason === null) return
    try {
      await api.cancelTask(taskId, reason)
      detail.reload()
      tasks.reload()
    } catch (error) {
      window.alert(error instanceof ApiError ? error.message : 'Не удалось отменить')
    }
  }

  async function createTask(event: React.FormEvent) {
    event.preventDefault()
    setSubmitError(null)
    let input: unknown = { text: inputText }
    if (!inputText.trim() && !advancedInput) {
      setSubmitError('Напишите, что нужно передать агенту.')
      return
    }
    if (advancedInput) {
      try {
        input = JSON.parse(inputJson)
      } catch {
        setSubmitError('Проверьте JSON. Пример: {"text":"Привет"}')
        return
      }
      if (!input || typeof input !== 'object' || Array.isArray(input)) {
        setSubmitError('Дополнительные данные должны быть JSON-объектом, например {"text":"Привет"}.')
        return
      }
    }
    setSubmitting(true)
    try {
      const task = await api.submitTask({ agent_id: selectedAgent, input_payload: input })
      navigate(`/tasks/${task.id}`)
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Не удалось создать задание')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <PageHead
        title="Центр задач"
        subtitle="Активные, ожидающие и завершённые задания. Жизненный цикл: очередь → аренда узла → выполнение → проверка."
        actions={
          user ? (
            <Link to="/agents" className="btn btn-sm">
              Выбрать агента
            </Link>
          ) : (
            <Link to="/login" state={{ from: '/tasks' }} className="btn btn-primary btn-sm">
              Войти или создать аккаунт
            </Link>
          )
        }
      />

      {user && !id ? (
        <form className="card quick-task-form" onSubmit={(event) => void createTask(event)}>
          <div className="card-head">
            <div>
              <h2 className="card-title">Отправить новое задание</h2>
              <div className="card-sub">Выберите опубликованного агента и введите данные. Результат появится на этой странице.</div>
            </div>
          </div>
          <div className="form-grid">
            <div className="field">
              <label>Кому отправить?</label>
              <select className="select" value={selectedAgent} onChange={(event) => setSelectedAgent(event.target.value)} required>
                <option value="">Выберите агента</option>
                {(publishedAgents.data?.items ?? []).map((agent) => <option key={agent.id} value={agent.id}>{agent.name} — {agent.summary}</option>)}
              </select>
              {publishedAgents.error ? <span className="faint small">Не удалось загрузить каталог. Обновите страницу.</span> : null}
              {!publishedAgents.loading && !publishedAgents.error && (publishedAgents.data?.items.length ?? 0) === 0 ? <span className="faint small">Сначала создайте и опубликуйте агента или подключитесь к каталогу.</span> : null}
            </div>
            <div className="field">
              <label>Что попросить агента сделать?</label>
              <textarea className="textarea quick-task-input" value={inputText} onChange={(event) => setInputText(event.target.value)} placeholder="Напишите текст или задачу обычными словами" />
              <span className="faint small">Обычный текст автоматически отправится агенту как поле «text».</span>
            </div>
          </div>
          <details className="advanced-node-settings">
            <summary>Дополнительно: задать входные данные вручную (JSON)</summary>
            <textarea className="textarea quick-task-input" value={inputJson} onChange={(event) => setInputJson(event.target.value)} spellCheck={false} style={{ marginTop: 10 }} />
            <label className="row small" style={{ marginTop: 8 }}>
              <input type="checkbox" checked={advancedInput} onChange={(event) => setAdvancedInput(event.target.checked)} />
              Использовать этот JSON вместо простого текста
            </label>
          </details>
          {submitError ? <div className="alert">{submitError}</div> : null}
          <div className="row between" style={{ flexWrap: 'wrap' }}>
            <span className="faint small">Чтобы задание реально выполнилось, нужен подключённый узел. <Link to="/infrastructure">Настроить узел</Link></span>
            <button className="btn btn-primary" type="submit" disabled={submitting || !selectedAgent || publishedAgents.loading}>
              {submitting ? 'Отправляю…' : 'Отправить задание'}
            </button>
          </div>
        </form>
      ) : null}

      {id ? (
        <div className="card" style={{ marginBottom: 20 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">Задание</h2>
              <div className="card-sub mono">{id}</div>
            </div>
            <div className="row">
              <Badge value={live ? 'ok' : 'warn'} text={live ? 'live события' : 'опрос'} />
              {detail.data ? <Badge value={detail.data.status} /> : null}
              {detail.data && !['succeeded', 'failed', 'cancelled', 'expired'].includes(detail.data.status) ? (
                <button className="btn btn-sm btn-danger" onClick={() => void cancelTask(detail.data!.id)}>
                  Отменить
                </button>
              ) : null}
            </div>
          </div>

          {detail.loading ? (
            <Loader rows={3} />
          ) : detail.error ? (
            <ErrorBox message={detail.error} onRetry={detail.reload} />
          ) : detail.data ? (
            <div className="two-col">
              <div className="stack">
                <dl className="kv">
                  <dt>Статус</dt>
                  <dd>
                    <Badge value={detail.data.status} />
                  </dd>
                  <dt>Попытки</dt>
                  <dd>
                    {detail.data.attempts} из {detail.data.max_attempts}
                  </dd>
                  <dt>Приоритет</dt>
                  <dd>{detail.data.priority}</dd>
                  <dt>Агент</dt>
                  <dd>
                    <Link to={`/agents/${detail.data.agent_id}`} className="mono">
                      {detail.data.agent_id.slice(0, 8)}…
                    </Link>
                  </dd>
                  <dt>Узел</dt>
                  <dd className="mono">{detail.data.node_id?.slice(0, 8) ?? '—'}</dd>
                  <dt>Дедлайн</dt>
                  <dd>{formatDate(detail.data.deadline_at)}</dd>
                  <dt>Завершено</dt>
                  <dd>{formatDate(detail.data.finished_at)}</dd>
                  <dt>hash результата</dt>
                  <dd className="mono" style={{ fontSize: 11.5 }}>
                    {detail.data.result_hash ?? '—'}
                  </dd>
                  <dt>Стоимость</dt>
                  <dd>{detail.data.cost_units}</dd>
                </dl>

                <div>
                  <div className="card-sub" style={{ marginBottom: 6 }}>
                    input_payload
                  </div>
                  <pre className="json">{JSON.stringify(detail.data.input_payload, null, 2)}</pre>
                </div>
                <div>
                  <div className="card-sub" style={{ marginBottom: 6 }}>
                    {detail.data.error ? 'error' : 'output_payload'}
                  </div>
                  <pre className="json">
                    {JSON.stringify(detail.data.error ?? detail.data.output_payload, null, 2)}
                  </pre>
                </div>
              </div>

              <div>
                <div className="card-sub" style={{ marginBottom: 10 }}>
                  События жизненного цикла
                </div>
                {events.length === 0 ? (
                  <div className="faint" style={{ fontSize: 13 }}>
                    Событий пока нет.
                  </div>
                ) : (
                  <div className="timeline">
                    {events.map((event) => (
                      <div className="timeline-item" key={`${event.sequence}-${event.event_type}`}>
                        <div
                          className={`timeline-dot ${
                            event.event_type.includes('fail') || event.event_type.includes('expire')
                              ? 'bad'
                              : event.event_type.includes('succeed')
                                ? 'ok'
                                : event.event_type.includes('retry')
                                  ? 'warn'
                                  : ''
                          }`}
                        />
                        <div>
                          <div className="timeline-title">{event.event_type}</div>
                          <div className="timeline-meta">
                            {event.actor} · {formatDate(event.created_at)}
                          </div>
                          {event.message ? (
                            <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>
                              {event.message}
                            </div>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="row" style={{ marginBottom: 14 }}>
        {STATUS_FILTERS.map((item) => (
          <button
            key={item.value}
            className={`btn btn-sm${status === item.value ? ' btn-primary' : ''}`}
            onClick={() => {
              setStatus(item.value)
              setPage(1)
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      {!user ? (
        <div className="alert info">Задания видны после входа: список привязан к вашему аккаунту.</div>
      ) : tasks.loading ? (
        <Loader rows={4} />
      ) : tasks.error ? (
        <ErrorBox message={tasks.error} onRetry={tasks.reload} />
      ) : tasks.data && tasks.data.items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Создано</th>
                  <th>Статус</th>
                  <th>Агент</th>
                  <th>Вход</th>
                  <th>Попытки</th>
                  <th>Результат</th>
                </tr>
              </thead>
              <tbody>
                {tasks.data.items.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <Link to={`/tasks/${task.id}`} className="mono">
                        {relativeTime(task.created_at)}
                      </Link>
                    </td>
                    <td>
                      <Badge value={task.status} />
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {task.agent_id.slice(0, 8)}…
                    </td>
                    <td className="faint" style={{ maxWidth: 240 }}>
                      {truncate(JSON.stringify(task.input_payload), 60)}
                    </td>
                    <td>
                      {task.attempts}/{task.max_attempts}
                    </td>
                    <td className="faint" style={{ maxWidth: 260 }}>
                      {task.error
                        ? truncate(String(task.error.message ?? 'ошибка'), 60)
                        : task.output_payload && Object.keys(task.output_payload).length > 0
                          ? truncate(JSON.stringify(task.output_payload), 60)
                          : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row between" style={{ marginTop: 16 }}>
            <span className="faint" style={{ fontSize: 12.5 }}>
              Всего {tasks.data.meta.total} · страница {tasks.data.meta.page} из {tasks.data.meta.pages}
            </span>
            <div className="row">
              <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage((v) => v - 1)}>
                ← Назад
              </button>
              <button
                className="btn btn-sm"
                disabled={page >= tasks.data.meta.pages}
                onClick={() => setPage((v) => v + 1)}
              >
                Вперёд →
              </button>
            </div>
          </div>
        </>
      ) : (
        <Empty>
          <p>Заданий нет.</p>
          <p className="faint" style={{ fontSize: 12.5 }}>
            Откройте карточку агента и отправьте первое задание.
          </p>
        </Empty>
      )}
    </>
  )
}
