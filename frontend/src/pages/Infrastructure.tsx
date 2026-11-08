import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api, type InfrastructureNode, type NodeCredentials } from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync, usePolling } from '@/lib/useAsync'
import { label, relativeTime } from '@/lib/format'
import { useAuth } from '@/lib/auth'

export default function Infrastructure() {
  const { user } = useAuth()
  const [mine, setMine] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [credentials, setCredentials] = useState<NodeCredentials | null>(null)
  const [selected, setSelected] = useState<InfrastructureNode | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const nodes = useAsync((signal) => api.nodes({ mine, page_size: 50 }, signal), [mine, user?.id])
  const heartbeats = useAsync(
    (signal) => (selected ? api.nodeHeartbeats(selected.id, signal) : Promise.resolve([])),
    [selected?.id],
  )

  usePolling(() => nodes.reload(), 8000, Boolean(user))

  const [form, setForm] = useState({
    name: '',
    kind: 'community',
    region: 'global',
    cpu_cores: 4,
    memory_mb: 2048,
    max_concurrency: 2,
    limit_cpu_seconds: 60,
    limit_wall_seconds: 120,
    limit_memory_mb: 1024,
    allow_network: false,
    capabilities: 'python',
  })

  if (!user) {
    return (
      <>
        <PageHead
          title="Инфраструктура"
          subtitle="Облачные узлы платформы и серверы участников: регистрация, heartbeat, лимиты и ёмкость."
        />
        <div className="alert info">
          Раздел доступен после входа. Узлы принадлежат участникам: каждый видит и обслуживает свои серверы.
        </div>
        <Link to="/login" state={{ from: '/infrastructure' }} className="btn btn-primary">
          Войти или создать аккаунт
        </Link>
      </>
    )
  }

  async function register() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.registerNode({
        ...form,
        capabilities: form.capabilities
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      })
      setCredentials(result.credentials)
      setShowForm(false)
      nodes.reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось зарегистрировать узел')
    } finally {
      setBusy(false)
    }
  }

  async function revoke(node: InfrastructureNode) {
    const reason = window.prompt('Причина отзыва узла?', 'выведен из эксплуатации')
    if (reason === null) return
    try {
      await api.revokeNode(node.id, reason)
      nodes.reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось отозвать узел')
    }
  }

  const items = nodes.data?.items ?? []
  const online = items.filter((node) => node.status === 'online').length
  const capacity = items.reduce((sum, node) => sum + node.max_concurrency, 0)
  const running = items.reduce((sum, node) => sum + node.running_tasks, 0)

  return (
    <>
      <PageHead
        title="Инфраструктура"
        subtitle="Узлы исполнения: облачные воркеры платформы и серверы участников. Каждый узел аутентифицируется HMAC-подписью, а не паролем."
        actions={
          <button className="btn btn-primary" onClick={() => setShowForm((value) => !value)}>
            {showForm ? 'Скрыть форму' : 'Подключить узел'}
          </button>
        }
      />

      <div className="grid grid-stats" style={{ marginBottom: 18 }}>
        <Stat label="Мои узлы" value={items.length} />
        <Stat label="Онлайн" value={online} hint={`${items.length - online} недоступно`} />
        <Stat label="Ёмкость слотов" value={capacity} hint={`${running} занято`} />
        <Stat
          label="Всего задач на узлах"
          value={items.reduce((sum, node) => sum + node.running_tasks, 0)}
          hint="в реальном времени"
        />
      </div>

      {error ? <div className="alert" style={{ marginBottom: 14 }}>{error}</div> : null}

      {credentials ? (
        <div className="card" style={{ marginBottom: 18, borderColor: 'rgba(52,211,153,0.4)' }}>
          <div className="card-head">
            <h2 className="card-title">Секрет узла (показывается один раз)</h2>
            <button className="btn btn-sm" onClick={() => setCredentials(null)}>
              Я сохранил
            </button>
          </div>
          <p className="muted" style={{ fontSize: 13 }}>
            {credentials.warning}. На сервере узла установите проект (Python 3.11+) и запустите команду из корня репозитория:
          </p>
          <pre className="json">{`python -m pip install -e backend
AC_NODE_KEY=${credentials.node_key} AC_NODE_SECRET=${credentials.node_secret} \\
python -m app.worker.node_worker --api-url https://<адрес-вашего-API>`}</pre>
        </div>
      ) : null}

      {showForm ? (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-head">
            <h2 className="card-title">Новый узел</h2>
            <span className="card-sub">Достаточно указать имя. Остальные настройки можно не менять.</span>
          </div>
          <div className="form-grid">
            <div className="field">
              <label>Имя</label>
              <input
                className="input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder="Мой сервер"
                required
              />
            </div>
          </div>
          <details className="advanced-node-settings">
            <summary>Дополнительные настройки (обычно менять не нужно)</summary>
            <div className="form-grid" style={{ marginTop: 12 }}>
            <div className="field">
              <label>Тип</label>
              <select
                className="select"
                value={form.kind}
                onChange={(event) => setForm({ ...form, kind: event.target.value })}
              >
                <option value="community">Сервер участника</option>
                <option value="cloud">Облачный</option>
                <option value="edge">Периферийный</option>
              </select>
            </div>
            <div className="field">
              <label>Регион</label>
              <input
                className="input"
                value={form.region}
                onChange={(event) => setForm({ ...form, region: event.target.value })}
              />
            </div>
            <div className="field">
              <label>CPU ядер</label>
              <input
                className="input"
                type="number"
                value={form.cpu_cores}
                onChange={(event) => setForm({ ...form, cpu_cores: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Память, МБ</label>
              <input
                className="input"
                type="number"
                value={form.memory_mb}
                onChange={(event) => setForm({ ...form, memory_mb: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Параллельных задач</label>
              <input
                className="input"
                type="number"
                value={form.max_concurrency}
                onChange={(event) => setForm({ ...form, max_concurrency: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Лимит CPU, сек</label>
              <input
                className="input"
                type="number"
                value={form.limit_cpu_seconds}
                onChange={(event) => setForm({ ...form, limit_cpu_seconds: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Лимит времени, сек</label>
              <input
                className="input"
                type="number"
                value={form.limit_wall_seconds}
                onChange={(event) => setForm({ ...form, limit_wall_seconds: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Лимит памяти, МБ</label>
              <input
                className="input"
                type="number"
                value={form.limit_memory_mb}
                onChange={(event) => setForm({ ...form, limit_memory_mb: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Возможности (через запятую)</label>
              <input
                className="input"
                value={form.capabilities}
                onChange={(event) => setForm({ ...form, capabilities: event.target.value })}
              />
            </div>
            </div>
            <label className="row" style={{ fontSize: 13, marginTop: 12 }}>
              <input
                type="checkbox"
                checked={form.allow_network}
                onChange={(event) => setForm({ ...form, allow_network: event.target.checked })}
              />
              Разрешить сетевой доступ задачам
            </label>
          </details>
          <div className="row between" style={{ marginTop: 16 }}>
            <span className="faint small">Сеть выключена по умолчанию — безопаснее для старта.</span>
            <button className="btn btn-primary" disabled={busy || !form.name} onClick={() => void register()}>
              {busy ? 'Регистрация…' : 'Зарегистрировать'}
            </button>
          </div>
        </div>
      ) : null}

      <div className="row" style={{ marginBottom: 12 }}>
        <button className={`btn btn-sm${mine ? ' btn-primary' : ''}`} onClick={() => setMine(true)}>
          Мои узлы
        </button>
        {user.role === 'admin' ? (
          <button className={`btn btn-sm${!mine ? ' btn-primary' : ''}`} onClick={() => setMine(false)}>
            Все узлы (админ)
          </button>
        ) : null}
      </div>

      {nodes.loading ? (
        <Loader rows={3} />
      ) : nodes.error ? (
        <ErrorBox message={nodes.error} onRetry={nodes.reload} />
      ) : items.length === 0 ? (
        <Empty>
          <p>Узлов нет.</p>
          <p className="faint" style={{ fontSize: 12.5 }}>
            Подключите узел и запустите worker — задания начнут выполняться.
          </p>
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Узел</th>
                <th>Тип</th>
                <th>Статус</th>
                <th>Нагрузка</th>
                <th>Лимиты</th>
                <th>Heartbeat</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((node) => (
                <tr key={node.id}>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => setSelected(node)}
                      style={{ padding: 0, background: 'none', border: 'none', fontWeight: 600 }}
                    >
                      {node.name}
                    </button>
                    <div className="faint mono" style={{ fontSize: 11 }}>
                      {node.node_key} · {node.region}
                    </div>
                  </td>
                  <td>{label(node.kind)}</td>
                  <td>
                    <div className="stack" style={{ gap: 4 }}>
                      <Badge value={node.status} />
                      <span className="faint" style={{ fontSize: 11 }}>
                        здоровье: {label(node.health)}
                      </span>
                    </div>
                  </td>
                  <td>
                    {node.running_tasks}/{node.max_concurrency}
                    <div className="reputation-bar" style={{ width: 70, marginTop: 4 }}>
                      <span
                        style={{
                          width: `${Math.min(100, (node.running_tasks / Math.max(1, node.max_concurrency)) * 100)}%`,
                        }}
                      />
                    </div>
                  </td>
                  <td className="faint" style={{ fontSize: 11.5 }}>
                    cpu {node.limit_cpu_seconds}s · ram {node.limit_memory_mb}MB
                    <br />
                    net: {node.allow_network ? 'разрешён' : 'запрещён'}
                  </td>
                  <td className="faint">{relativeTime(node.last_seen_at)}</td>
                  <td>
                    {node.status !== 'revoked' ? (
                      <button className="btn btn-sm btn-danger" onClick={() => void revoke(node)}>
                        Отозвать
                      </button>
                    ) : (
                      <span className="faint" style={{ fontSize: 11.5 }}>
                        {node.revoked_reason}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected ? (
        <div className="modal-backdrop" onClick={() => setSelected(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="card-head">
              <div>
                <h2 className="card-title">{selected.name}</h2>
                <div className="card-sub mono">{selected.node_key}</div>
              </div>
              <button className="btn btn-sm" onClick={() => setSelected(null)}>
                Закрыть
              </button>
            </div>
            <dl className="kv" style={{ marginBottom: 16 }}>
              <dt>Возможности</dt>
              <dd>{selected.capabilities.join(', ') || '—'}</dd>
              <dt>Доверие</dt>
              <dd>уровень {selected.trust_level}</dd>
              <dt>Верифицирован</dt>
              <dd>{selected.verified_at ? 'да' : 'нет'}</dd>
              <dt>Endpoint</dt>
              <dd className="mono">{selected.endpoint_url ?? '—'}</dd>
            </dl>
            <div className="card-sub" style={{ marginBottom: 8 }}>
              История heartbeat
            </div>
            {heartbeats.loading ? (
              <Loader rows={2} />
            ) : (heartbeats.data ?? []).length === 0 ? (
              <div className="faint" style={{ fontSize: 13 }}>
                Узел ещё не присылал heartbeat.
              </div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Время</th>
                      <th>Задач</th>
                      <th>CPU</th>
                      <th>Память</th>
                      <th>Здоровье</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(heartbeats.data ?? []).slice(0, 10).map((beat) => (
                      <tr key={beat.id}>
                        <td className="mono">{beat.sequence}</td>
                        <td className="faint">{relativeTime(beat.created_at)}</td>
                        <td>{beat.running_tasks}</td>
                        <td>{beat.cpu_load}%</td>
                        <td>{beat.memory_used_mb} MB</td>
                        <td>
                          <Badge value={beat.health} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </>
  )
}
