import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  type AgentMessage,
  type ConversationDetail,
  type ConversationSummary,
} from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { relativeTime, truncate } from '@/lib/format'

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'open', label: 'Открытые' },
  { value: 'closed', label: 'Закрытые' },
]

export default function Conversations() {
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const list = useAsync(
    (signal) => api.conversations({ status: status || undefined, page, page_size: 15 }, signal),
    [status, page],
  )
  const stats = useAsync((signal) => api.messageStats(signal), [])
  const detail = useAsync(
    (signal) => (selectedId ? api.conversation(selectedId, signal) : Promise.resolve(null)),
    [selectedId],
  )

  const [live, setLive] = useState(false)
  const [liveMessages, setLiveMessages] = useState<AgentMessage[]>([])

  // Живые сообщения диалога: WebSocket, при недоступности — обычный рендер без потока
  useEffect(() => {
    setLiveMessages([])
    setLive(false)
    if (!selectedId) return

    // Access-токен в query-строке больше не используется: браузер берёт
    // одноразовый тикет (30 с), обычным запросом с заголовком Authorization.
    let socket: WebSocket | null = null
    let cancelled = false
    void (async () => {
      let ticket: string
      try {
        ticket = (await api.wsTicket()).ticket
      } catch {
        return
      }
      if (cancelled) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${window.location.host}/api/v1/ws/conversations/${selectedId}?ticket=${encodeURIComponent(ticket)}`
      try {
        socket = new WebSocket(url)
        socket.onopen = () => setLive(true)
        socket.onmessage = (event) => {
          const payload = JSON.parse(event.data as string) as {
            type: string
            message_id?: string
            kind?: string
            status?: string
            payload?: Record<string, unknown>
            attempts?: number
            event_type?: string
          }
          if (payload.type !== 'event' || !payload.message_id) return
          const messageId = payload.message_id
          setLiveMessages((current) => {
            const next = current.filter((item) => item.id !== messageId)
            next.push({
              id: messageId,
              conversation_id: selectedId,
              sender_agent_id: null,
              recipient_agent_id: null,
              kind: (payload.kind as AgentMessage['kind']) ?? 'event',
              strategy: 'inbox',
              status: (payload.status as AgentMessage['status']) ?? 'pending',
              correlation_id: null,
              payload: payload.payload ?? {},
              response_payload: {},
              error: null,
              error_message: null,
              attempts: payload.attempts ?? 0,
              max_attempts: 0,
              latency_ms: null,
              delivered_at: null,
              created_at: new Date().toISOString(),
            })
            return next
          })
        }
        socket.onclose = () => setLive(false)
      } catch {
        setLive(false)
      }
    })()
    return () => {
      cancelled = true
      socket?.close()
      setLive(false)
    }
  }, [selectedId])

  const items = list.data?.items ?? []

  return (
    <div className="stack">
      <PageHead
        title="Диалоги агентов"
        subtitle="Обмен сообщениями между агентами (A2A): очередь доставки, повторы, MCP-вызовы"
        actions={
          <Link to="/agents" className="btn btn-sm">
            Каталог агентов
          </Link>
        }
      />

      {stats.data ? (
        <div className="grid grid-4">
          <Stat label="В очереди" value={stats.data.pending ?? 0} hint="ожидают доставки" />
          <Stat label="В работе" value={stats.data.processing ?? 0} hint="арендованы воркером" />
          <Stat label="Доставлено" value={stats.data.delivered ?? 0} hint="at-least-once" />
          <Stat
            label="Ошибки"
            value={(stats.data.failed ?? 0) + (stats.data.expired ?? 0)}
            hint="исчерпаны попытки или истёк срок"
          />
        </div>
      ) : null}

      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        {STATUS_FILTERS.map((item) => (
          <button
            key={item.value}
            className={`btn btn-sm ${status === item.value ? 'btn-primary' : ''}`}
            onClick={() => {
              setStatus(item.value)
              setPage(1)
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(280px, 1fr) 1.4fr', gap: 16 }}>
        <div className="card">
          <div className="card-head">
            <h2 className="card-title">Мои диалоги</h2>
          </div>
          {list.loading ? <Loader rows={4} /> : null}
          {list.error ? <ErrorBox message={list.error} onRetry={list.reload} /> : null}
          {!list.loading && items.length === 0 ? (
            <Empty>
              Диалогов пока нет. Отправьте сообщение со страницы агента — диалог создастся
              автоматически.
            </Empty>
          ) : null}
          <div className="stack">
            {items.map((conversation: ConversationSummary) => (
              <button
                key={conversation.id}
                className={`list-row ${selectedId === conversation.id ? 'active' : ''}`}
                onClick={() => setSelectedId(conversation.id)}
              >
                <div className="row between">
                  <strong>{truncate(conversation.subject || 'Без темы', 42)}</strong>
                  <Badge value={conversation.status} />
                </div>
                <div className="muted small">
                  {conversation.message_count} сообщ. ·{' '}
                  {conversation.last_message_at
                    ? relativeTime(conversation.last_message_at)
                    : 'нет сообщений'}{' '}
                  · участников: {conversation.participant_ids.length}
                </div>
              </button>
            ))}
          </div>
          {list.data && list.data.meta.pages > 1 ? (
            <div className="row between" style={{ marginTop: 12 }}>
              <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                Назад
              </button>
              <span className="muted small">
                {page} / {list.data.meta.pages}
              </span>
              <button
                className="btn btn-sm"
                disabled={page >= list.data.meta.pages}
                onClick={() => setPage(page + 1)}
              >
                Вперёд
              </button>
            </div>
          ) : null}
        </div>

        <div className="card">
          <div className="card-head">
            <h2 className="card-title">История сообщений</h2>
            {selectedId ? (
              <span className={`badge ${live ? 'ok' : 'idle'}`}>
                <span className="dot" />
                {live ? 'live' : 'снимок'}
              </span>
            ) : null}
          </div>
          {!selectedId ? <Empty>Выберите диалог слева</Empty> : null}
          {detail.loading ? <Loader rows={3} /> : null}
          {detail.error ? <ErrorBox message={detail.error} onRetry={detail.reload} /> : null}
          {detail.data ? (
            <MessageList detail={detail.data} liveMessages={liveMessages} />
          ) : null}
        </div>
      </div>
    </div>
  )
}

function MessageList({
  detail,
  liveMessages,
}: {
  detail: ConversationDetail
  liveMessages: AgentMessage[]
}) {
  const seen = new Set(detail.messages.map((message) => message.id))
  const messages = [
    ...detail.messages,
    ...liveMessages.filter((message) => !seen.has(message.id)),
  ]

  if (messages.length === 0) {
    return <Empty>Сообщений в диалоге нет</Empty>
  }

  return (
    <div className="stack">
      {detail.task_id ? (
        <Link to={`/tasks/${detail.task_id}`} className="muted small">
          Связано с заданием {detail.task_id.slice(0, 8)}…
        </Link>
      ) : null}
      {messages.map((message) => (
        <div key={message.id} className="bubble">
          <div className="row between">
            <span className="row" style={{ gap: 6 }}>
              <strong>{message.kind}</strong>
              <span className="muted small">{message.strategy}</span>
            </span>
            <Badge value={message.status} />
          </div>
          <pre className="code-block">{JSON.stringify(message.payload, null, 2)}</pre>
          {Object.keys(message.response_payload ?? {}).length > 0 ? (
            <pre className="code-block">{JSON.stringify(message.response_payload, null, 2)}</pre>
          ) : null}
          {message.error_message ? <div className="muted small">⚠ {message.error_message}</div> : null}
          <div className="muted small">
            попыток: {message.attempts}/{message.max_attempts || '—'}
            {message.latency_ms !== null ? ` · ${message.latency_ms} ms` : ''} ·{' '}
            {relativeTime(message.created_at)}
          </div>
        </div>
      ))}
    </div>
  )
}
