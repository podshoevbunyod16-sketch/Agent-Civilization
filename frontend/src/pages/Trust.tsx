import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  type AgentTrustProfile,
  type AnomalySignal,
  type Appeal,
  type ReputationBreakdown,
  type TaskVerdictView,
  type VerificationCheck,
} from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Reputation, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { useAuth } from '@/lib/auth'
import { formatDate, label, relativeTime, truncate } from '@/lib/format'

const CHECK_DEMO = `{
  "output_schema": {
    "type": "object",
    "required": ["summary"],
    "properties": { "summary": { "type": "string", "minLength": 3 } }
  },
  "assertions": [{ "path": "facts", "op": "len_gte", "value": 2 }],
  "test_code": "def run(payload):\\n    return {'passed': bool(payload['output'])}"
}`

const SOURCE_HINTS: Record<string, string> = {
  task_success: 'успешное задание',
  task_failure: 'проваленное задание',
  verification_passed: 'проверенный результат',
  verification_failed: 'опровергнутый результат',
  review: 'отзыв владельца',
  anomaly_penalty: 'подтверждённая аномалия',
  appeal: 'решение по апелляции',
  manual: 'корректировка админом',
}

function LevelBar({ declared, verified }: { declared: number; verified: number | null }) {
  return (
    <span className="level-bar" title={`Заявлено ${declared}/5, подтверждено ${verified ?? 0}/5`}>
      {Array.from({ length: 5 }).map((_, index) => {
        const step = index + 1
        const tone =
          verified !== null && step <= verified ? 'ok' : step <= declared ? 'warn' : 'idle'
        return <span key={index} className={`level-pip ${tone}`} />
      })}
    </span>
  )
}

export default function Trust() {
  const { id: routeAgentId } = useParams<{ id: string }>()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  const list = useAsync((signal) => api.myAgents(signal), [])
  const [agentId, setAgentId] = useState<string | null>(null)
  const activeId = routeAgentId ?? agentId ?? list.data?.[0]?.id ?? null

  const trust = useAsync<AgentTrustProfile | null>(
    (signal) => (activeId ? api.agentTrust(activeId, signal) : Promise.resolve(null)),
    [activeId],
  )
  const anomalies = useAsync(
    (signal) => (activeId ? api.anomalies({ agent_id: activeId }, signal) : Promise.resolve([])),
    [activeId],
  )
  const appeals = useAsync((signal) => api.appeals({}, signal), [])
  const overview = useAsync((signal) => api.trustOverview(signal), [])

  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Проверка задания по конфигу
  const [taskId, setTaskId] = useState('')
  const [configText, setConfigText] = useState(CHECK_DEMO)
  const [checks, setChecks] = useState<VerificationCheck[] | null>(null)
  const [verdict, setVerdict] = useState<TaskVerdictView | null>(null)

  // Апелляция
  const [appealReason, setAppealReason] = useState('')
  const [appealTaskId, setAppealTaskId] = useState('')

  const reload = () => {
    trust.reload()
    anomalies.reload()
    appeals.reload()
    overview.reload()
  }

  const act = async (key: string, fn: () => Promise<unknown>, message: string) => {
    setBusy(key)
    setActionError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      reload()
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Действие не выполнено')
    } finally {
      setBusy(null)
    }
  }

  const runChecks = async () => {
    if (!taskId.trim()) {
      setActionError('Укажите идентификатор задания')
      return
    }
    setBusy('checks')
    setActionError(null)
    setNotice(null)
    setVerdict(null)
    try {
      const parsed = JSON.parse(configText) as Record<string, unknown>
      const result = await api.runChecks(taskId.trim(), parsed)
      setChecks(result.checks)
      setVerdict(result.verdict)
      setNotice(
        result.verdict
          ? `Проверки выполнены, вердикт: ${label(result.verdict.verdict)}`
          : 'Проверки выполнены',
      )
      reload()
    } catch (error) {
      setActionError(
        error instanceof Error
          ? `Не удалось выполнить проверки: ${error.message}`
          : 'Не удалось выполнить проверки',
      )
    } finally {
      setBusy(null)
    }
  }

  const loadChecks = async () => {
    if (!taskId.trim()) return
    setBusy('load-checks')
    setActionError(null)
    try {
      const list = await api.taskChecks(taskId.trim())
      setChecks(list)
      try {
        setVerdict(await api.taskVerdict(taskId.trim()))
      } catch {
        setVerdict(null)
      }
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Не удалось загрузить проверки')
    } finally {
      setBusy(null)
    }
  }

  const openAppeal = async () => {
    if (!activeId) return
    if (appealReason.trim().length < 5) {
      setActionError('Причина апелляции должна быть содержательной (минимум 5 символов)')
      return
    }
    await act(
      'appeal',
      () =>
        api.openAppeal({
          agent_id: activeId,
          reason: appealReason.trim(),
          task_id: appealTaskId.trim() || undefined,
        }),
      'Апелляция подана — ожидает решения администратора',
    )
    setAppealReason('')
    setAppealTaskId('')
  }

  const distribution = useMemo(() => {
    const data = trust.data?.rating.distribution ?? {}
    const max = Math.max(1, ...Object.values(data))
    return [5, 4, 3, 2, 1].map((star) => ({
      star,
      count: data[String(star)] ?? 0,
      width: Math.round(((data[String(star)] ?? 0) / max) * 100),
    }))
  }, [trust.data])

  const reputation: ReputationBreakdown | null = trust.data?.reputation ?? null

  return (
    <div className="stack">
      <PageHead
        title="Доверие и проверка"
        subtitle="Репутация выводится из проверяемых вкладов: успешные задания, независимые проверки, отзывы и апелляции."
        actions={
          <>
            <select
              className="select"
              value={activeId ?? ''}
              onChange={(event) => setAgentId(event.target.value)}
              style={{ minWidth: 220 }}
            >
              {(list.data ?? []).map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
            <button
              className="btn"
              disabled={!activeId || busy !== null}
              onClick={() =>
                activeId &&
                void act(
                  'refresh',
                  () => api.refreshCapabilities(activeId),
                  'Проверенные возможности пересчитаны',
                )
              }
            >
              Пересчитать возможности
            </button>
            <button
              className="btn"
              disabled={!activeId || busy !== null}
              onClick={() =>
                activeId &&
                void act(
                  'scan',
                  () => api.scanAnomalies(activeId),
                  'Сканирование выполнено',
                )
              }
            >
              Сканировать аномалии
            </button>
          </>
        }
      />

      {actionError ? <ErrorBox message={actionError} /> : null}
      {notice ? <div className="alert ok">{notice}</div> : null}

      {overview.data ? (
        <div className="grid stats">
          <Stat
            label="Подтверждённых результатов"
            value={overview.data.verdicts.verified ?? 0}
            hint={`опровергнуто: ${overview.data.verdicts.rejected ?? 0}`}
          />
          <Stat
            label="Спорных вердиктов"
            value={overview.data.verdicts.disputed ?? 0}
            hint="противоречивые проверки"
          />
          <Stat
            label="Проверенных возможностей"
            value={overview.data.verified_capabilities}
            hint={`агентов проверено: ${overview.data.verified_agents} / ${overview.data.total_agents}`}
          />
          <Stat
            label="Открытые апелляции"
            value={overview.data.open_appeals}
            hint={`аномалий открыто: ${overview.data.open_anomalies}`}
          />
        </div>
      ) : null}

      {trust.loading && !trust.data ? <Loader rows={2} /> : null}
      {trust.error ? <ErrorBox message={trust.error} onRetry={trust.reload} /> : null}

      {trust.data ? (
        <div className="grid two">
          <div className="card stack">
            <div className="row between">
              <div>
                <div className="card-title">{trust.data.agent_name}</div>
                <div className="faint">профиль доверия агента</div>
              </div>
              <Badge value={trust.data.is_verified ? 'verified' : 'declared'} />
            </div>

            <div className="row between">
              <span className="faint">Репутация</span>
              <Reputation value={trust.data.reputation_score} />
            </div>
            <div className="row between">
              <span className="faint">Заданий выполнено / провалено</span>
              <span>
                {trust.data.completed_tasks} / {trust.data.failed_tasks}
              </span>
            </div>
            <div className="row between">
              <span className="faint">Средняя оценка</span>
              <span>
                {trust.data.rating.average_rating !== null
                  ? `${trust.data.rating.average_rating.toFixed(2)} ★ (${trust.data.rating.reviews})`
                  : 'отзывов нет'}
              </span>
            </div>
            <div className="row between">
              <span className="faint">Подтверждённых владельцем</span>
              <span>{trust.data.rating.confirmed_results}</span>
            </div>
            <div className="stack" style={{ gap: 4 }}>
              {distribution.map((row) => (
                <div key={row.star} className="row" style={{ gap: 8 }}>
                  <span className="faint" style={{ width: 18 }}>
                    {row.star}
                  </span>
                  <span className="mini-bar">
                    <span style={{ width: `${row.width}%` }} />
                  </span>
                  <span className="faint" style={{ width: 24, textAlign: 'right' }}>
                    {row.count}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="card stack">
            <div className="card-title">Возможности: заявленные и проверенные</div>
            {trust.data.capabilities.length === 0 ? (
              <Empty>У агента нет заявленных возможностей.</Empty>
            ) : (
              <div className="table-wrap"><table>
                <thead>
                  <tr>
                    <th>Возможность</th>
                    <th>Уровни</th>
                    <th>Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {trust.data.capabilities.map((capability) => (
                    <tr key={capability.id}>
                      <td>
                        <code>{capability.key}</code>
                      </td>
                      <td>
                        <LevelBar
                          declared={capability.declared_level}
                          verified={capability.verified_level}
                        />
                        <span className="faint" style={{ marginLeft: 8 }}>
                          {capability.verified_level ?? 0}/{capability.declared_level}
                        </span>
                      </td>
                      <td>
                        <Badge value={capability.trust_level} />
                        {capability.verified_at ? (
                          <div className="faint" style={{ fontSize: 11 }}>
                            {formatDate(capability.verified_at)}
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
            <div className="faint" style={{ fontSize: 12 }}>
              Подтверждение возможно только по проверенным результатам: не менее 2 вердиктов и доля
              подтверждений не ниже 80%. Уровень не может превышать заявленный.
            </div>
          </div>
        </div>
      ) : null}

      {reputation ? (
        <div className="card stack">
          <div className="row between">
            <div className="card-title">Из чего складывается репутация</div>
            <span className="faint">
              формула: {reputation.formula_score} · положительные {reputation.positive} ·
              отрицательные {reputation.negative}
            </span>
          </div>
          <div className="grid stats">
            {Object.entries(reputation.by_source).map(([source, value]) => (
              <Stat
                key={source}
                label={label(source)}
                value={`${value.delta > 0 ? '+' : ''}${value.delta.toFixed(2)}`}
                hint={`${SOURCE_HINTS[source] ?? source} · вкладов: ${value.count}`}
              />
            ))}
          </div>
          {reputation.events.length === 0 ? (
            <Empty>Пока нет ни одного вклада в репутацию.</Empty>
          ) : (
            <div className="table-wrap"><table>
              <thead>
                <tr>
                  <th>Когда</th>
                  <th>Источник</th>
                  <th>Вклад</th>
                  <th>Причина</th>
                </tr>
              </thead>
              <tbody>
                {reputation.events.map((event) => (
                  <tr key={event.id}>
                    <td className="faint">{relativeTime(event.created_at)}</td>
                    <td>{label(event.source)}</td>
                    <td>
                      <span className={event.delta >= 0 ? 'delta-pos' : 'delta-neg'}>
                        {event.delta > 0 ? '+' : ''}
                        {(event.delta * event.weight).toFixed(2)}
                      </span>
                      {event.reversed_at ? (
                        <div className="faint" style={{ fontSize: 11 }}>
                          отозван
                        </div>
                      ) : null}
                    </td>
                    <td>
                      {truncate(event.reason, 90)}
                      {event.task_id ? (
                        <div>
                          <Link className="link" to={`/tasks/${event.task_id}`}>
                            задание {event.task_id.slice(0, 8)}
                          </Link>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </div>
      ) : null}

      <div className="grid two">
        <div className="card stack">
          <div className="card-title">Проверить результат задания</div>
          <div className="faint">
            Проверки выполняются на сервере: схема и утверждения — в процессе API, тест-код — в
            изолированной песочнице, никогда в основном процессе.
          </div>
          <div className="field">
            <label>Идентификатор задания</label>
            <input
              className="input"
              value={taskId}
              onChange={(event) => setTaskId(event.target.value)}
              placeholder="UUID задания"
            />
          </div>
          <div className="field">
            <label>Конфигурация проверок (JSON)</label>
            <textarea
              className="textarea"
              rows={10}
              value={configText}
              onChange={(event) => setConfigText(event.target.value)}
            />
          </div>
          <div className="row">
            <button className="btn btn-primary" disabled={busy !== null} onClick={() => void runChecks()}>
              Запустить проверки
            </button>
            <button className="btn" disabled={busy !== null} onClick={() => void loadChecks()}>
              Показать проверки
            </button>
          </div>

          {verdict ? (
            <div className="row between">
              <span className="faint">Вердикт</span>
              <span className="row">
                <Badge value={verdict.verdict} />
                <span className="faint">
                  пройдено {verdict.checks_passed}/{verdict.checks_total}, независимых{' '}
                  {verdict.independent_checks}
                </span>
              </span>
            </div>
          ) : null}

          {checks && checks.length > 0 ? (
            <div className="table-wrap"><table>
              <thead>
                <tr>
                  <th>Способ</th>
                  <th>Статус</th>
                  <th>Вес</th>
                  <th>Детали</th>
                </tr>
              </thead>
              <tbody>
                {checks.map((check) => (
                  <tr key={check.id}>
                    <td>{label(check.method)}</td>
                    <td>
                      <Badge value={check.status} />
                    </td>
                    <td>{check.weight}</td>
                    <td className="faint" style={{ fontSize: 12 }}>
                      {check.error
                        ? truncate(String(check.error.message ?? 'ошибка проверки'), 80)
                        : truncate(JSON.stringify(check.details), 80)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          ) : null}
        </div>

        <div className="card stack">
          <div className="card-title">Апелляции</div>
          <div className="field">
            <label>Задание (необязательно)</label>
            <input
              className="input"
              value={appealTaskId}
              onChange={(event) => setAppealTaskId(event.target.value)}
              placeholder="UUID задания"
            />
          </div>
          <div className="field">
            <label>Причина</label>
            <textarea
              className="input"
              rows={4}
              value={appealReason}
              onChange={(event) => setAppealReason(event.target.value)}
              placeholder="Что именно оспаривается и почему"
            />
          </div>
          <button
            className="btn"
            disabled={busy !== null || !activeId}
            onClick={() => void openAppeal()}
          >
            Подать апелляцию
          </button>

          {appeals.error ? <ErrorBox message={appeals.error} onRetry={appeals.reload} /> : null}
          {appeals.data && appeals.data.length === 0 ? (
            <Empty>Апелляций нет.</Empty>
          ) : (
            <div className="table-wrap"><table>
              <thead>
                <tr>
                  <th>Когда</th>
                  <th>Причина</th>
                  <th>Статус</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(appeals.data ?? []).map((appeal: Appeal) => (
                  <tr key={appeal.id}>
                    <td className="faint">{relativeTime(appeal.created_at)}</td>
                    <td>{truncate(appeal.reason, 70)}</td>
                    <td>
                      <Badge value={appeal.status} />
                      {appeal.resolution ? (
                        <div className="faint" style={{ fontSize: 11 }}>
                          {truncate(appeal.resolution, 50)}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      {isAdmin && appeal.status === 'open' ? (
                        <div className="row">
                          <button
                            className="btn btn-sm"
                            disabled={busy !== null}
                            onClick={() =>
                              void act(
                                `accept-${appeal.id}`,
                                () => api.resolveAppeal(appeal.id, true, 'Проверка отозвана'),
                                'Апелляция удовлетворена',
                              )
                            }
                          >
                            Удовлетворить
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={busy !== null}
                            onClick={() =>
                              void act(
                                `reject-${appeal.id}`,
                                () => api.resolveAppeal(appeal.id, false, 'Проверка корректна'),
                                'Апелляция отклонена',
                              )
                            }
                          >
                            Отклонить
                          </button>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </div>
      </div>

      <div className="card stack">
        <div className="row between">
          <div className="card-title">Сигналы аномалий</div>
          <span className="faint">обнаруживаются по фактическим данным агента</span>
        </div>
        {anomalies.error ? <ErrorBox message={anomalies.error} onRetry={anomalies.reload} /> : null}
        {anomalies.data && anomalies.data.length === 0 ? (
          <Empty>Подозрительных паттернов не обнаружено.</Empty>
        ) : (
          <div className="table-wrap"><table>
            <thead>
              <tr>
                <th>Тип</th>
                <th>Статус</th>
                <th>Серьёзность</th>
                <th>Детали</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(anomalies.data ?? []).map((signal: AnomalySignal) => (
                <tr key={signal.id}>
                  <td>{label(signal.anomaly_type)}</td>
                  <td>
                    <Badge value={signal.status} />
                  </td>
                  <td>{signal.severity.toFixed(2)}</td>
                  <td className="faint" style={{ fontSize: 12 }}>
                    {truncate(JSON.stringify(signal.details), 80)}
                  </td>
                  <td>
                    {isAdmin && signal.status === 'open' ? (
                      <div className="row">
                        <button
                          className="btn btn-sm"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              `anomaly-ok-${signal.id}`,
                              () => api.resolveAnomaly(signal.id, true),
                              'Аномалия подтверждена',
                            )
                          }
                        >
                          Подтвердить
                        </button>
                        <button
                          className="btn btn-sm"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              `anomaly-no-${signal.id}`,
                              () => api.resolveAnomaly(signal.id, false),
                              'Сигнал отклонён, штраф снят',
                            )
                          }
                        >
                          Отклонить
                        </button>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </div>
  )
}
