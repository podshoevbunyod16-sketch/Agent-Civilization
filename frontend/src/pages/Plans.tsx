import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type PlanDetail, type PlanEvent, type PlanStep, type PlanSummary } from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { relativeTime, truncate } from '@/lib/format'

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'draft', label: 'Черновики' },
  { value: 'planned', label: 'Спланированы' },
  { value: 'running', label: 'Выполняются' },
  { value: 'succeeded', label: 'Выполнены' },
  { value: 'failed', label: 'С ошибками' },
]

/** Порядок шагов для отображения: сначала процессы, потом завершённые. */
const STEP_ORDER: Record<string, number> = {
  running: 0,
  queued: 1,
  ready: 2,
  pending: 3,
  blocked: 4,
  failed: 5,
  skipped: 6,
  succeeded: 7,
}

export default function Plans() {
  const { id } = useParams<{ id: string }>()
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)

  const list = useAsync(
    (signal) => api.plans({ status: status || undefined, page, page_size: 15 }, signal),
    [status, page],
  )
  const stats = useAsync((signal) => api.planStats(signal), [])
  const detail = useAsync(
    (signal) => (id ? api.plan(id, signal) : Promise.resolve(null)),
    [id],
  )

  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const reloadAll = () => {
    list.reload()
    stats.reload()
    detail.reload()
  }

  const act = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label)
    setActionError(null)
    try {
      await action()
      reloadAll()
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Не удалось выполнить действие')
    } finally {
      setBusy(null)
    }
  }

  const items = list.data?.items ?? []

  return (
    <div className="stack">
      <PageHead
        title="Оркестрация"
        subtitle="Составные задания: декомпозиция на шаги, зависимости, подбор агентов и запуск"
        actions={
          <Link to="/tasks" className="btn btn-sm">
            Центр задач
          </Link>
        }
      />

      {stats.data ? (
        <div className="grid grid-4">
          <Stat label="Планов" value={stats.data.plans_total} hint="всего создано" />
          <Stat
            label="Выполняются"
            value={stats.data.plans.running ?? 0}
            hint="в работе сейчас"
          />
          <Stat
            label="Завершено"
            value={
              (stats.data.plans.succeeded ?? 0) + (stats.data.plans.failed ?? 0)
            }
            hint="успешно или с ошибками"
          />
          <Stat
            label="Шагов готово"
            value={stats.data.steps.ready ?? 0}
            hint="ждут запуска"
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

      <div className="grid" style={{ gridTemplateColumns: 'minmax(280px, 1fr) 1.6fr', gap: 16 }}>
        <div className="card">
          <div className="card-head">
            <h2 className="card-title">Мои планы</h2>
          </div>
          {list.loading ? <Loader rows={4} /> : null}
          {list.error ? <ErrorBox message={list.error} onRetry={list.reload} /> : null}
          {!list.loading && items.length === 0 ? (
            <Empty>
              Планов пока нет. План создаётся из API (`POST /plans`) или агентом-планировщиком
              через A2A.
            </Empty>
          ) : null}
          <div className="stack">
            {items.map((plan: PlanSummary) => (
              <Link
                key={plan.id}
                to={`/plans/${plan.id}`}
                className={`list-row ${id === plan.id ? 'active' : ''}`}
                style={{ textDecoration: 'none' }}
              >
                <div className="row between">
                  <strong>{truncate(plan.title, 38)}</strong>
                  <Badge value={plan.status} />
                </div>
                <div className="muted small">
                  параллелизм {plan.max_parallelism} · {relativeTime(plan.created_at)}
                </div>
              </Link>
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
            <h2 className="card-title">План</h2>
            {detail.data ? (
              <span className="row" style={{ gap: 8 }}>
                <button
                  className="btn btn-sm btn-primary"
                  disabled={busy !== null || ['succeeded', 'failed', 'cancelled'].includes(detail.data.status)}
                  onClick={() => act('run', () => api.runPlan(detail.data!.id))}
                >
                  {busy === 'run' ? 'Запуск…' : 'Запустить'}
                </button>
                <button
                  className="btn btn-sm"
                  disabled={busy !== null || ['succeeded', 'failed', 'cancelled'].includes(detail.data.status)}
                  onClick={() => act('cancel', () => api.cancelPlan(detail.data!.id, 'через UI'))}
                >
                  Отменить
                </button>
              </span>
            ) : null}
          </div>

          {!id ? <Empty>Выберите план слева</Empty> : null}
          {detail.loading ? <Loader rows={3} /> : null}
          {detail.error ? <ErrorBox message={detail.error} onRetry={detail.reload} /> : null}
          {actionError ? <ErrorBox message={actionError} /> : null}

          {detail.data ? (
            <PlanView
              plan={detail.data}
              busy={busy}
              onRetryStep={(key) => act(`retry:${key}`, () => api.retryPlanStep(detail.data!.id, key))}
            />
          ) : null}
        </div>
      </div>
    </div>
  )
}

function PlanView({
  plan,
  busy,
  onRetryStep,
}: {
  plan: PlanDetail
  busy: string | null
  onRetryStep: (key: string) => void
}) {
  const steps = useMemo(
    () => [...plan.steps].sort((a, b) => (STEP_ORDER[a.status] ?? 9) - (STEP_ORDER[b.status] ?? 9)),
    [plan.steps],
  )
  const done = plan.steps.filter((step) => step.status === 'succeeded').length
  const percent = plan.steps.length ? Math.round((done / plan.steps.length) * 100) : 0

  return (
    <div className="stack">
      <div className="row between">
        <div>
          <strong>{plan.title}</strong>
          <div className="muted small">
            шагов: {plan.steps.length} · выполнено: {done} · политика:{' '}
            {plan.failure_policy === 'fail_plan' ? 'остановить план' : 'продолжать'}
          </div>
        </div>
        <Badge value={plan.status} />
      </div>

      <div className="progress">
        <span style={{ width: `${percent}%` }} />
      </div>

      {plan.error ? (
        <div className="alert">
          {String((plan.error as { message?: string }).message ?? 'План завершился с ошибкой')}
        </div>
      ) : null}

      {steps.map((step) => (
        <StepCard
          key={step.id}
          step={step}
          busy={busy === `retry:${step.key}`}
          onRetry={() => onRetryStep(step.key)}
        />
      ))}

      <PlanTimeline events={plan.events} />
    </div>
  )
}

function StepCard({
  step,
  busy,
  onRetry,
}: {
  step: PlanStep
  busy: boolean
  onRetry: () => void
}) {
  const canRetry = ['failed', 'skipped', 'blocked'].includes(step.status)
  return (
    <div className="bubble">
      <div className="row between">
        <span className="row" style={{ gap: 8 }}>
          <strong>{step.name}</strong>
          <span className="tag">{step.key}</span>
        </span>
        <Badge value={step.status} />
      </div>

      {step.depends_on.length > 0 ? (
        <div className="muted small">зависит от: {step.depends_on.join(', ')}</div>
      ) : null}
      {step.required_capabilities.length > 0 ? (
        <div className="muted small">
          возможности: {step.required_capabilities.join(', ')}
          {step.match_score !== null ? ` · соответствие ${(step.match_score * 100).toFixed(0)}%` : ''}
        </div>
      ) : null}
      {step.note ? <div className="muted small">⚠ {step.note}</div> : null}
      {Object.keys(step.output_payload ?? {}).length > 0 ? (
        <pre className="code-block">{JSON.stringify(step.output_payload, null, 2)}</pre>
      ) : null}

      <div className="row between">
        <span className="muted small">
          попыток: {step.attempts}/{step.max_attempts}
          {step.duration_ms !== null ? ` · ${step.duration_ms} ms` : ''}
          {step.agent_id ? (
            <>
              {' · '}
              <Link to={`/agents/${step.agent_id}`}>исполнитель</Link>
            </>
          ) : null}
          {step.task_id ? (
            <>
              {' · '}
              <Link to={`/tasks/${step.task_id}`}>задание</Link>
            </>
          ) : null}
        </span>
        {canRetry ? (
          <button className="btn btn-sm" disabled={busy} onClick={onRetry}>
            {busy ? 'Повтор…' : 'Повторить'}
          </button>
        ) : null}
      </div>
    </div>
  )
}

function PlanTimeline({ events }: { events: PlanEvent[] }) {
  if (events.length === 0) return null
  const recent = [...events].slice(-12).reverse()

  return (
    <details className="stack">
      <summary className="muted small">Журнал событий ({events.length})</summary>
      <div className="stack" style={{ marginTop: 8 }}>
        {recent.map((event) => (
          <div key={event.sequence} className="row between muted small">
            <span>
              <span className="tag">{event.event_type}</span>{' '}
              {event.step_key ? `[${event.step_key}] ` : ''}
              {truncate(event.message, 70)}
            </span>
            <span>{relativeTime(event.created_at)}</span>
          </div>
        ))}
      </div>
    </details>
  )
}
