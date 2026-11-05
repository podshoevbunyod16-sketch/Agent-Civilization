import { useState } from 'react'
import { api, type MaintenanceRun, type OpsHealth } from '@/api/client'
import { Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { formatDate, relativeTime } from '@/lib/format'

const SEVERITY_CLASS: Record<string, string> = {
  critical: 'bad',
  high: 'bad',
  medium: 'warn',
  low: 'idle',
}

const GAUGE_LABELS: Record<string, string> = {
  tasks_total: 'Всего заданий',
  tasks_queued: 'В очереди',
  tasks_running: 'В работе',
  tasks_succeeded: 'Успешно',
  tasks_failed: 'С ошибкой',
  tasks_stuck_leases: 'Истёкших аренд',
  tasks_overdue: 'Просроченных',
  queue_oldest_age_seconds: 'Возраст старейшего задания, с',
  messages_pending: 'Сообщений в очереди',
  messages_failed: 'Недоставленных сообщений',
  nodes_online: 'Узлов online',
  nodes_stale: 'Узлов без heartbeat',
  held_budgets_count: 'Замороженных бюджетов',
  held_budget_units: 'Заморожено, ACU',
  ledger_total_units: 'Сумма балансов (должна быть 0)',
  compat_checks_passed: 'Совместимость: успешно',
  compat_checks_failed: 'Совместимость: провалов',
  integrations_total: 'Интеграций',
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export default function Ops() {
  const health = useAsync<OpsHealth>((signal) => api.opsHealth(signal), [])
  const tables = useAsync((signal) => api.opsTables(signal), [])

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [run, setRun] = useState<MaintenanceRun | null>(null)

  const maintenance = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.runMaintenance()
      setRun(result)
      health.reload()
      tables.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Обслуживание не выполнено')
    } finally {
      setBusy(false)
    }
  }

  const gauges = health.data?.gauges ?? {}

  return (
    <div className="stack">
      <PageHead
        title="Эксплуатация"
        subtitle="Наблюдаемость и обслуживание распределённой цивилизации: очередь, аренды, узлы, журнал. Метрики отдаются в формате Prometheus; состояние считается из базы, поэтому одинаково на всех репликах."
        actions={
          <>
            <button className="btn" disabled={busy} onClick={maintenance}>
              Проход обслуживания
            </button>
            <button
              className="btn"
              onClick={() => {
                health.reload()
                tables.reload()
              }}
            >
              Обновить
            </button>
          </>
        }
      />

      {error ? <ErrorBox message={error} /> : null}
      {run ? (
        <div className="alert ok">
          Обслуживание: возвращено аренд {run.requeued_tasks}, узлов offline{' '}
          {run.nodes_offline}, истекло сообщений {run.expired_messages}, {run.duration_ms} мс
        </div>
      ) : null}

      {health.loading && !health.data ? <Loader rows={2} /> : null}
      {health.error ? <ErrorBox message={health.error} onRetry={health.reload} /> : null}

      {health.data ? (
        <>
          <div className="grid stats">
            <Stat
              label="Состояние"
              value={health.data.status}
              hint={`проверено ${relativeTime(health.data.checked_at)}`}
            />
            <Stat
              label="Очередь"
              value={String(gauges.tasks_queued ?? 0)}
              hint={`старейшее ждёт ${Math.round(gauges.queue_oldest_age_seconds ?? 0)} с`}
            />
            <Stat
              label="Узлы online"
              value={String(gauges.nodes_online ?? 0)}
              hint={`без heartbeat: ${gauges.nodes_stale ?? 0}`}
            />
            <Stat
              label="Журнал"
              value={`${(gauges.ledger_total_units ?? 0).toFixed(2)} ACU`}
              hint="сумма всех балансов обязана быть 0"
            />
          </div>

          <div className="card stack">
            <div className="card-title">Предупреждения</div>
            {health.data.warnings.length === 0 ? (
              <Empty>Предупреждений нет: очередь, узлы и журнал в норме.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Код</th>
                      <th>Важность</th>
                      <th>Что делать</th>
                    </tr>
                  </thead>
                  <tbody>
                    {health.data.warnings.map((warning) => (
                      <tr key={warning.code}>
                        <td className="mono">{warning.code}</td>
                        <td>
                          <span className={`badge ${SEVERITY_CLASS[warning.severity] ?? 'idle'}`}>
                            {warning.severity}
                          </span>
                        </td>
                        <td>{warning.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      ) : null}

      <div className="grid two">
        <div className="card stack">
          <div className="card-title">Показатели</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>Значение</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(gauges).map(([key, value]) => (
                  <tr key={key}>
                    <td>{GAUGE_LABELS[key] ?? key}</td>
                    <td>{Number.isInteger(value) ? value : value.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card stack">
          <div className="card-title">Процесс и база</div>
          {health.data ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Параметр</th>
                    <th>Значение</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Версия Python</td>
                    <td>{health.data.process.python}</td>
                  </tr>
                  <tr>
                    <td>Потоков</td>
                    <td>{health.data.process.threads}</td>
                  </tr>
                  <tr>
                    <td>Память (RSS)</td>
                    <td>{health.data.process.max_rss_mb} MB</td>
                  </tr>
                  <tr>
                    <td>Аптайм</td>
                    <td>{Math.round(health.data.process.uptime_seconds)} с</td>
                  </tr>
                  <tr>
                    <td>Соединений к БД</td>
                    <td>
                      {health.data.database.active_connections} /{' '}
                      {health.data.database.max_connections}
                    </td>
                  </tr>
                  <tr>
                    <td>Размер БД</td>
                    <td>{bytes(health.data.database.size_bytes)}</td>
                  </tr>
                  <tr>
                    <td>Задержка запроса к БД</td>
                    <td>{health.data.database.latency_ms} мс</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <Loader rows={2} />
          )}
          <div className="faint" style={{ fontSize: 12 }}>
            Метрики: <span className="mono">/metrics</span> (Prometheus, требует
            сервисный токен). Демон обслуживания: <span className="mono">
              python -m app.worker.maintenance
            </span>{' '}
            — запускается отдельным процессом и выполняет те же операции, что кнопка
            выше.
          </div>
        </div>
      </div>

      <div className="card stack">
        <div className="row between">
          <div className="card-title">Размеры таблиц</div>
          <span className="faint">
            всего строк: {tables.data ? tables.data.total_rows : '—'} ·{' '}
            {tables.data ? formatDate(new Date().toISOString()) : ''}
          </span>
        </div>
        {tables.loading && !tables.data ? <Loader rows={2} /> : null}
        {tables.error ? <ErrorBox message={tables.error} onRetry={tables.reload} /> : null}
        {tables.data ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Таблица</th>
                  <th>Строк</th>
                </tr>
              </thead>
              <tbody>
                {tables.data.tables.slice(0, 20).map((row) => (
                  <tr key={row.table}>
                    <td className="mono">{row.table}</td>
                    <td>{row.rows}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <div className="faint" style={{ fontSize: 12 }}>
          Логический бэкап: <span className="mono">GET /api/v1/ops/backup</span> — gzip
          с JSON Lines и контрольной суммой. Это не замена <span className="mono">pg_dump</span>{' '}
          и не PITR: согласованности между таблицами нет.
        </div>
      </div>
    </div>
  )
}
