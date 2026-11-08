import { Link } from 'react-router-dom'
import { api } from '@/api/client'
import { Badge, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import AgentCard from '@/components/AgentCard'
import { useAsync, usePolling } from '@/lib/useAsync'
import { formatDate, label, relativeTime } from '@/lib/format'
import { useAuth } from '@/lib/auth'

export default function Dashboard() {
  const { user } = useAuth()
  const stats = useAsync((signal) => api.stats(signal), [])
  const agents = useAsync((signal) => api.agents({ page_size: 6, sort: 'reputation' }, signal), [])
  const health = useAsync((signal) => api.health(signal), [])

  usePolling(() => {
    stats.reload()
    health.reload()
  }, 15000)

  const s = stats.data

  return (
    <>
      <PageHead
        title="Центр цивилизации"
        subtitle="Единая точка обзора: агенты, задачи и инфраструктура. Все показатели получены из работающего API — демонстрационных данных нет."
        actions={
          <>
            {health.data ? (
              <Badge
                value={health.data.status === 'ok' ? 'online' : 'degraded'}
                text={`API: ${health.data.status}`}
              />
            ) : null}
            <Link to="/developer/agents" className="btn btn-primary">
              Создать агента
            </Link>
          </>
        }
      />

      <section className="card quick-start-home">
        <div className="card-head">
          <div>
            <h2 className="card-title">Начните с трёх простых шагов</h2>
            <div className="card-sub">Не нужно настраивать всю систему сразу.</div>
          </div>
        </div>
        <div className="quick-start-steps">
          <Link to="/developer/agents" className="quick-start-step">
            <span className="quick-step-number">1</span>
            <span><strong>Создайте агента</strong><small>Заполните карточку и опубликуйте</small></span>
          </Link>
          <Link to="/tasks" className="quick-start-step">
            <span className="quick-step-number">2</span>
            <span><strong>Поставьте задание</strong><small>Выберите агента и посмотрите результат</small></span>
          </Link>
          <Link to="/mcp" className="quick-start-step">
            <span className="quick-step-number">3</span>
            <span><strong>Подключите через MCP</strong><small>Поручайте работу AI-помощнику</small></span>
          </Link>
        </div>
      </section>

      {stats.error ? <ErrorBox message={stats.error} onRetry={stats.reload} /> : null}

      <div className="grid grid-stats" style={{ marginBottom: 18 }}>
        <Stat
          label="Агентов в реестре"
          value={s ? s.agents.total : '—'}
          hint={s ? `${s.agents.published} опубликовано` : undefined}
        />
        <Stat
          label="Заданий"
          value={s ? s.tasks.total : '—'}
          hint={s ? `${s.tasks.by_status.succeeded ?? 0} выполнено` : undefined}
        />
        <Stat
          label="Узлов инфраструктуры"
          value={s ? s.nodes.total : '—'}
          hint={s ? `${s.nodes.by_status.online ?? 0} онлайн, ёмкость ${s.nodes.online_capacity}` : undefined}
        />
        <Stat
          label="Участников"
          value={s ? s.users : '—'}
          hint="разработчиков и операторов"
        />
      </div>

      <div className="two-col">
        <section>
          <div className="card-head">
            <div>
              <h2 className="card-title">Агенты с высокой репутацией</h2>
              <div className="card-sub">Рейтинг формируется по подтверждённым задачам</div>
            </div>
            <Link to="/agents" className="btn btn-sm">
              Весь каталог
            </Link>
          </div>

          {agents.loading ? (
            <Loader rows={3} />
          ) : agents.error ? (
            <ErrorBox message={agents.error} onRetry={agents.reload} />
          ) : agents.data && agents.data.items.length > 0 ? (
            <div className="grid grid-cards">
              {agents.data.items.map((agent) => (
                <AgentCard key={agent.id} agent={agent} />
              ))}
            </div>
          ) : (
            <div className="card empty">
              <p>В реестре пока нет опубликованных агентов.</p>
              <p className="faint" style={{ fontSize: 12.5 }}>
                Создайте агента в панели разработчика, добавьте версию и опубликуйте — он появится здесь.
              </p>
            </div>
          )}
        </section>

        <aside className="stack">
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Состояние системы</h2>
              {health.data ? <Badge value={health.data.status === 'ok' ? 'ok' : 'warn'} text={health.data.status} /> : null}
            </div>
            {health.loading ? (
              <Loader rows={2} />
            ) : health.error ? (
              <ErrorBox message={health.error} onRetry={health.reload} />
            ) : (
              <dl className="kv">
                <dt>Версия</dt>
                <dd>{health.data?.version}</dd>
                <dt>Окружение</dt>
                <dd>{health.data?.environment}</dd>
                {Object.entries(health.data?.checks ?? {}).map(([key, value]) => (
                  <div key={key} style={{ display: 'contents' }}>
                    <dt>{key}</dt>
                    <dd>
                      <Badge value={value === 'ok' ? 'ok' : 'warn'} text={value} />
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Задачи по статусам</h2>
            </div>
            {s && Object.keys(s.tasks.by_status).length > 0 ? (
              <div className="stack" style={{ gap: 8 }}>
                {Object.entries(s.tasks.by_status).map(([status, count]) => (
                  <div key={status} className="row between">
                    <Badge value={status} />
                    <span className="mono">{count}</span>
                  </div>
                ))}
                {s.tasks.oldest_queued_at ? (
                  <div className="card-sub" style={{ marginTop: 6 }}>
                    Самое старое задание в очереди: {formatDate(s.tasks.oldest_queued_at)}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="faint" style={{ fontSize: 13 }}>
                Заданий ещё не было.
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Узлы</h2>
              <Link to="/infrastructure" className="btn btn-sm">
                Мониторинг
              </Link>
            </div>
            {s && Object.keys(s.nodes.by_status).length > 0 ? (
              <div className="stack" style={{ gap: 8 }}>
                {Object.entries(s.nodes.by_status).map(([status, count]) => (
                  <div key={status} className="row between">
                    <span className="muted">{label(status)}</span>
                    <span className="mono">{count}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="faint" style={{ fontSize: 13 }}>
                Узлы не зарегистрированы. Подключите worker, чтобы выполнять задачи.
              </div>
            )}
          </div>

          {user ? (
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Ваш профиль</h2>
              </div>
              <dl className="kv">
                <dt>Роль</dt>
                <dd>{user.role}</dd>
                <dt>Последний вход</dt>
                <dd>{relativeTime(user.last_login_at)}</dd>
                <dt>С нами с</dt>
                <dd>{formatDate(user.created_at)}</dd>
              </dl>
            </div>
          ) : (
            <div className="alert info">
              Вы просматриваете каталог как гость. Войдите, чтобы создавать агентов и отправлять задачи.
            </div>
          )}
        </aside>
      </div>
    </>
  )
}
