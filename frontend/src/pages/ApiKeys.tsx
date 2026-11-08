import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type ApiKeyCreated } from '@/api/client'
import { ErrorBox, Loader, PageHead } from '@/components/ui'
import { useAuth } from '@/lib/auth'
import { useAsync } from '@/lib/useAsync'

export default function ApiKeys() {
  const { user } = useAuth()
  const keys = useAsync(
    (signal) => (user ? api.apiKeys(signal) : Promise.resolve([])),
    [user?.id],
  )
  const [name, setName] = useState('MCP клиент')
  const [expiresInDays, setExpiresInDays] = useState(90)
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  if (!user) {
    return (
      <>
        <PageHead title="API-ключи" subtitle="Создавайте и отзывайте ключи для MCP и внешних приложений." />
        <div className="card stack">
          <p>Войдите в аккаунт, чтобы управлять своими ключами.</p>
          <Link to="/login" state={{ from: '/settings/api-keys' }} className="btn btn-primary">Войти или создать аккаунт</Link>
        </div>
      </>
    )
  }

  async function createKey(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setCopied(false)
    try {
      const result = await api.createApiKey(name.trim(), expiresInDays)
      setCreated(result)
      keys.reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось создать ключ')
    } finally {
      setBusy(false)
    }
  }

  async function copyKey() {
    if (!created) return
    try {
      await navigator.clipboard.writeText(created.api_key)
      setCopied(true)
    } catch {
      setCopied(false)
      setError('Не удалось скопировать автоматически. Выделите и скопируйте ключ вручную.')
    }
  }

  async function revokeKey(id: string) {
    if (!window.confirm('Отозвать этот ключ? Подключённые с его помощью клиенты сразу потеряют доступ.')) return
    setError(null)
    try {
      await api.revokeApiKey(id)
      if (created?.id === id) setCreated(null)
      keys.reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось отозвать ключ')
    }
  }

  return (
    <>
      <PageHead
        title="API-ключи для MCP"
        subtitle="Создайте отдельный ключ для AI-клиента. Его можно отозвать здесь в любой момент."
        actions={<Link to="/mcp" className="btn btn-sm">Настроить MCP</Link>}
      />

      <div className="stack api-key-page">
        <form className="card stack" onSubmit={(event) => void createKey(event)}>
          <div>
            <h2 className="card-title">Создать ключ</h2>
            <p className="muted small">Ключ действует 90 дней по умолчанию. Полный секрет показывается только один раз.</p>
            <div className="alert info">Ключ работает только с MCP-интерфейсом и использует ваши права в нём; обычные REST API-эндпоинты закрыты для ключа. Создавайте отдельный ключ для каждого AI-клиента и отзывайте его, если перестали им пользоваться.</div>
          </div>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="api-key-name">Название подключения</label>
              <input id="api-key-name" className="input" value={name} onChange={(event) => setName(event.target.value)} minLength={2} maxLength={80} required />
            </div>
            <div className="field">
              <label htmlFor="api-key-expiry">Срок действия</label>
              <select id="api-key-expiry" className="select" value={expiresInDays} onChange={(event) => setExpiresInDays(Number(event.target.value))}>
                <option value={30}>30 дней</option>
                <option value={90}>90 дней</option>
                <option value={180}>180 дней</option>
                <option value={365}>1 год</option>
              </select>
            </div>
          </div>
          {error ? <ErrorBox message={error} /> : null}
          <div><button className="btn btn-primary" type="submit" disabled={busy || name.trim().length < 2}>{busy ? 'Создаю…' : 'Создать API-ключ'}</button></div>
        </form>

        {created ? (
          <section className="card api-key-created">
            <div className="card-head">
              <h2 className="card-title">Скопируйте новый ключ сейчас</h2>
              <button className="btn btn-sm" onClick={() => setCreated(null)}>Готово</button>
            </div>
            <div className="alert info">После закрытия этого сообщения секрет нельзя будет посмотреть ещё раз. Если потеряете его — отзовите ключ и создайте новый.</div>
            <code className="api-key-secret">{created.api_key}</code>
            <button className="btn btn-primary" onClick={() => void copyKey()}>{copied ? 'Скопировано' : 'Скопировать ключ'}</button>
            <p className="faint small">В MCP-настройках вставьте значение в заголовок: <code>Authorization: Bearer {created.api_key.slice(0, 16)}…</code></p>
          </section>
        ) : null}

        <section className="card stack">
          <div className="card-head">
            <div>
              <h2 className="card-title">Ваши ключи</h2>
              <div className="card-sub">Секреты не хранятся и повторно не показываются.</div>
            </div>
          </div>
          {keys.loading ? <Loader rows={2} /> : null}
          {keys.error ? <ErrorBox message={keys.error} onRetry={keys.reload} /> : null}
          {!keys.loading && !keys.error && (keys.data?.length ?? 0) === 0 ? <p className="faint small">Ключей пока нет. Создайте ключ для первого MCP-подключения.</p> : null}
          {(keys.data ?? []).map((key) => (
            <div className="api-key-row" key={key.id}>
              <div className="api-key-meta">
                <strong>{key.name}</strong>
                <code>{key.key_prefix}…</code>
                <span className="faint small">{key.revoked_at ? 'Отозван' : `Истекает: ${new Date(key.expires_at).toLocaleDateString()}`}</span>
              </div>
              {!key.revoked_at ? <button className="btn btn-sm btn-danger" onClick={() => void revokeKey(key.id)}>Отозвать</button> : <span className="badge bad">отозван</span>}
            </div>
          ))}
        </section>
      </div>
    </>
  )
}
