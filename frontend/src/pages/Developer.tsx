import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  type CompatCheck,
  type EcosystemStats,
  type Integration,
  type ProjectTemplate,
} from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { formatDate, label, relativeTime, truncate } from '@/lib/format'

const KIND_OPTIONS = [
  'ai_provider',
  'data_source',
  'messaging',
  'automation',
  'observability',
  'custom',
]

const PROTOCOL_OPTIONS = ['http', 'mcp', 'webhook', 'openapi']

const KIND_LABELS: Record<string, string> = {
  ai_provider: 'Провайдер моделей',
  data_source: 'Источник данных',
  messaging: 'Связь и уведомления',
  automation: 'Автоматизация',
  observability: 'Наблюдаемость',
  custom: 'Другое',
}

function statusTone(status: string): string {
  if (status === 'passed') return 'ok'
  if (status === 'failed') return 'bad'
  if (status === 'error') return 'bad'
  return 'idle'
}

export default function Developer() {
  const portal = useAsync((signal) => api.portal(signal), [])
  const stats = useAsync<EcosystemStats>((signal) => api.ecosystemStats(signal), [])
  const templates = useAsync((signal) => api.templates(undefined, signal), [])
  const integrations = useAsync((signal) => api.integrations({}, signal), [])

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [report, setReport] = useState<CompatCheck | null>(null)

  // Форма интеграции
  const [form, setForm] = useState({
    slug: '',
    name: '',
    summary: '',
    kind: 'ai_provider',
    protocol: 'http',
    base_url: '',
  })

  const selected: ProjectTemplate | null =
    (templates.data ?? []).find((item) => item.slug === selectedSlug) ?? null

  const act = async (key: string, fn: () => Promise<unknown>, message: string) => {
    setBusy(key)
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      portal.reload()
      stats.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Операция не выполнена')
    } finally {
      setBusy(null)
    }
  }

  const runCheck = (agentId: string) => {
    void act(
      `check:${agentId}`,
      async () => {
        const result = await api.compatCheck(agentId)
        setReport(result)
        if (result.status !== 'passed') {
          throw new Error(
            `Проверка не пройдена: ${result.checks
              .filter((item) => item.status === 'failed' || item.status === 'error')
              .map((item) => item.message)
              .join('; ')}`,
          )
        }
        return result
      },
      'Совместимость подтверждена',
    )
  }

  const addIntegration = () => {
    if (!form.slug || !form.name || !form.base_url) {
      setError('Заполните slug, название и адрес')
      return
    }
    void act(
      'integration',
      () =>
        api.addIntegration({
          slug: form.slug,
          name: form.name,
          summary: form.summary,
          kind: form.kind,
          protocol: form.protocol,
          base_url: form.base_url,
        }),
      'Интеграция добавлена в каталог',
    )
    setForm({ ...form, slug: '', name: '', summary: '', base_url: '' })
    integrations.reload()
  }

  return (
    <div className="stack">
      <PageHead
        title="Developer Portal"
        subtitle="Всё, что нужно стороннему разработчику: шаблоны проектов, проверка совместимости агента и каталог интеграций. Контракт — HTTP/JSON и MCP, поэтому клиент можно писать на любом языке."
        actions={
          <button
            className="btn"
            onClick={() => {
              portal.reload()
              templates.reload()
              integrations.reload()
              stats.reload()
            }}
          >
            Обновить
          </button>
        }
      />

      {error ? <ErrorBox message={error} /> : null}
      {notice ? <div className="alert ok">{notice}</div> : null}

      <div className="grid stats">
        <Stat
          label="Мои агенты"
          value={portal.data ? String(portal.data.agents_total) : '—'}
          hint={`совместимых: ${portal.data?.agents_ready ?? 0}`}
        />
        <Stat
          label="Шаблоны проектов"
          value={templates.data ? String(templates.data.length) : '—'}
          hint="python, http, docker, mcp"
        />
        <Stat
          label="Интеграции"
          value={integrations.data ? String(integrations.data.length) : '—'}
          hint="внешние платформы в каталоге"
        />
        <Stat
          label="Проверок совместимости"
          value={stats.data ? String(stats.data.checks.total) : '—'}
          hint="реальных прогонов контракта"
        />
      </div>

      <div className="card stack">
        <div className="card-title">Мои агенты и совместимость</div>
        <div className="faint">
          Проверка исполняет агента в песочнице на примере входа и сверяет выход со схемой.
          Заявленная возможность не становится подтверждённой: для этого нужен вердикт по
          результату (этап «Доверие»).
        </div>
        {portal.loading && !portal.data ? <Loader rows={2} /> : null}
        {portal.error ? <ErrorBox message={portal.error} onRetry={portal.reload} /> : null}
        {portal.data && portal.data.agents.length === 0 ? (
          <Empty>
            Агентов пока нет. Создайте первого в <Link className="link" to="/agents">каталоге</Link>{' '}
            или начните с шаблона ниже.
          </Empty>
        ) : null}
        {portal.data && portal.data.agents.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Агент</th>
                  <th>Статус</th>
                  <th>Версия</th>
                  <th>Последняя проверка</th>
                  <th>Негативный тест</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {portal.data.agents.map((agent) => (
                  <tr key={agent.agent_id}>
                    <td>
                      <Link className="link" to={`/agents/${agent.agent_id}`}>
                        {agent.name}
                      </Link>
                    </td>
                    <td>
                      <Badge value={agent.status} />
                    </td>
                    <td>{agent.current_version}</td>
                    <td>
                      {agent.last_check ? (
                        <span>
                          <Badge value={agent.last_check.status} />{' '}
                          <span className="faint">{agent.last_check.score.toFixed(1)}</span>
                        </span>
                      ) : (
                        <span className="faint">не проверялся</span>
                      )}
                    </td>
                    <td>
                      {agent.last_negative_check ? (
                        <Badge value={agent.last_negative_check.status} />
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td>
                      <button
                        className="btn btn-sm"
                        disabled={busy !== null}
                        onClick={() => runCheck(agent.agent_id)}
                      >
                        Проверить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {report ? (
          <div className="stack" style={{ marginTop: 8 }}>
            <div className="row between">
              <div className="card-title">
                Отчёт проверки: {report.status} · {report.score.toFixed(1)} ·{' '}
                {report.duration_ms?.toFixed(0) ?? '—'} ms
              </div>
              <button className="btn btn-sm" onClick={() => setReport(null)}>
                Закрыть
              </button>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Проверка</th>
                    <th>Результат</th>
                    <th>Сообщение</th>
                  </tr>
                </thead>
                <tbody>
                  {report.checks.map((item, index) => (
                    <tr key={`${item.kind}-${index}`}>
                      <td>{item.kind}</td>
                      <td>
                        <span className={`badge ${statusTone(item.status)}`}>{item.status}</span>
                      </td>
                      <td className="faint">{item.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {report.sample_output ? (
              <pre className="code">{JSON.stringify(report.sample_output, null, 2)}</pre>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="card stack">
        <div className="card-title">Шаблоны проектов</div>
        {templates.loading && !templates.data ? <Loader rows={2} /> : null}
        {templates.error ? <ErrorBox message={templates.error} onRetry={templates.reload} /> : null}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Шаблон</th>
                <th>Среда</th>
                <th>Описание</th>
                <th>Возможности</th>
                <th>Скачиваний</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(templates.data ?? []).map((item) => (
                <tr key={item.slug}>
                  <td>
                    <span className="mono">{item.slug}</span>
                  </td>
                  <td>{item.runtime}</td>
                  <td className="faint">{truncate(item.summary, 70)}</td>
                  <td className="faint" style={{ fontSize: 12 }}>
                    {item.requires_capabilities.join(', ') || '—'}
                  </td>
                  <td>{item.downloads}</td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => setSelectedSlug(item.slug === selectedSlug ? null : item.slug)}
                    >
                      {item.slug === selectedSlug ? 'Свернуть' : 'Файлы'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {selected ? (
          <div className="stack">
            <div className="row between">
              <div className="card-title">{selected.name}</div>
              <span className="faint">
                развернуть: <span className="mono">ac scaffold {selected.slug} ./my-agent</span>
              </span>
            </div>
            <div className="grid two">
              {selected.files.map((file) => (
                <div key={file.path} className="stack">
                  <div className="faint mono" style={{ fontSize: 12 }}>
                    {file.path}
                  </div>
                  <pre className="code">{file.content}</pre>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <div className="grid two">
        <div className="card stack">
          <div className="card-title">Каталог интеграций</div>
          <div className="faint">
            Платформа не привязана к одному AI-провайдеру: любой сервис подключается как
            интеграция. Секреты остаются у владельца, здесь хранится только публичный манифест.
          </div>
          {integrations.loading && !integrations.data ? <Loader rows={2} /> : null}
          {integrations.error ? (
            <ErrorBox message={integrations.error} onRetry={integrations.reload} />
          ) : null}
          {integrations.data && integrations.data.length === 0 ? (
            <Empty>Интеграций пока нет.</Empty>
          ) : null}
          {integrations.data && integrations.data.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Название</th>
                    <th>Тип</th>
                    <th>Доступность</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {integrations.data.map((item: Integration) => (
                    <tr key={item.id}>
                      <td>
                        <div>{item.name}</div>
                        <div className="faint mono" style={{ fontSize: 11 }}>
                          {item.slug} · {item.protocol}
                        </div>
                      </td>
                      <td>{KIND_LABELS[item.kind] ?? label(item.kind)}</td>
                      <td>
                        {item.is_verified ? (
                          <Badge value="ok" text="доступна" />
                        ) : item.last_checked_at ? (
                          <Badge value="bad" text="недоступна" />
                        ) : (
                          <span className="faint">не проверялась</span>
                        )}
                        {item.last_latency_ms ? (
                          <div className="faint" style={{ fontSize: 11 }}>
                            {item.last_latency_ms.toFixed(0)} ms ·{' '}
                            {relativeTime(item.last_checked_at ?? '')}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <div className="row">
                          <button
                            className="btn btn-sm"
                            disabled={busy !== null}
                            onClick={() =>
                              act(
                                `probe:${item.id}`,
                                () => api.probeIntegration(item.id),
                                'Проверка доступности выполнена',
                              )
                            }
                          >
                            Проверить
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={busy !== null}
                            onClick={() =>
                              act(
                                `del:${item.id}`,
                                () => api.deleteIntegration(item.id),
                                'Интеграция удалена',
                              ).then(() => integrations.reload())
                            }
                          >
                            Удалить
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>

        <div className="card stack">
          <div className="card-title">Добавить интеграцию</div>
          <div className="form-grid">
            <div className="field">
              <label>Slug</label>
              <input
                className="input"
                value={form.slug}
                onChange={(event) => setForm({ ...form, slug: event.target.value })}
                placeholder="my-provider"
              />
            </div>
            <div className="field">
              <label>Название</label>
              <input
                className="input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder="Мой провайдер моделей"
              />
            </div>
            <div className="field">
              <label>Тип</label>
              <select
                className="select"
                value={form.kind}
                onChange={(event) => setForm({ ...form, kind: event.target.value })}
              >
                {KIND_OPTIONS.map((kind) => (
                  <option key={kind} value={kind}>
                    {KIND_LABELS[kind]}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Протокол</label>
              <select
                className="select"
                value={form.protocol}
                onChange={(event) => setForm({ ...form, protocol: event.target.value })}
              >
                {PROTOCOL_OPTIONS.map((protocol) => (
                  <option key={protocol} value={protocol}>
                    {protocol}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>Адрес (http/https)</label>
            <input
              className="input"
              value={form.base_url}
              onChange={(event) => setForm({ ...form, base_url: event.target.value })}
              placeholder="https://llm.example.com/v1"
            />
          </div>
          <div className="field">
            <label>Короткое описание</label>
            <input
              className="input"
              value={form.summary}
              onChange={(event) => setForm({ ...form, summary: event.target.value })}
              placeholder="Чем полезна интеграция"
            />
          </div>
          <button className="btn btn-primary" disabled={busy !== null} onClick={addIntegration}>
            Добавить в каталог
          </button>
          <div className="faint" style={{ fontSize: 12 }}>
            Проверка доступности выполняется с сервера, поэтому адреса внутренней сети
            запрещены. Частные интеграции видны только владельцу.
          </div>

          {stats.data ? (
            <div className="stack" style={{ marginTop: 8 }}>
              <div className="card-title">Экосистема</div>
              <div className="faint" style={{ fontSize: 12 }}>
                агентов {stats.data.agents.total} · проверок {stats.data.checks.total} · заданий{' '}
                {stats.data.tasks} · шаблонов {stats.data.templates}
              </div>
              <div className="faint" style={{ fontSize: 12 }}>
                проверки:{' '}
                {Object.entries(stats.data.checks.by_status)
                  .map(([key, value]) => `${key} ${value}`)
                  .join(' · ') || '—'}
              </div>
              <div className="faint" style={{ fontSize: 12 }}>
                обновлено {stats.data ? formatDate(new Date().toISOString()) : ''}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
