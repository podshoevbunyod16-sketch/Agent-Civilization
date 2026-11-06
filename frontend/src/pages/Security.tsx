import { useEffect, useState } from 'react'
import {
  api,
  type AccountSecurity,
  type ApiKeyInfo,
  type SessionInfo,
  type TotpSetup,
} from '@/api/client'
import { Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { relativeTime } from '@/lib/format'

/**
 * Безопасность аккаунта (этап 10, B): подтверждение адреса, 2FA,
 * устройства и API-ключи. Все действия выполняются на реальном API:
 * никаких локальных «переключателей», которые ничего не меняют на сервере.
 */
export default function Security() {
  const security = useAsync<AccountSecurity>((signal) => api.accountSecurity(signal), [])
  const sessions = useAsync<SessionInfo[]>((signal) => api.sessions(signal), [])
  const keys = useAsync<ApiKeyInfo[]>((signal) => api.apiKeys(signal), [])

  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [setup, setSetup] = useState<TotpSetup | null>(null)
  const [code, setCode] = useState('')
  const [recovery, setRecovery] = useState<string[] | null>(null)
  const [keyName, setKeyName] = useState('')
  const [newKey, setNewKey] = useState<string | null>(null)

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 6000)
    return () => clearTimeout(timer)
  }, [notice])

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Действие не выполнено')
    } finally {
      setBusy(false)
    }
  }

  const requestVerification = () =>
    run(async () => {
      const result = await api.requestEmailVerification()
      if (result.dev_token) {
        // dev-режим доставки писем: ссылка не уходит, токен можно применить сразу
        await api.verifyEmail(result.dev_token)
        setNotice('Адрес подтверждён (dev-режим: письмо не отправлялось)')
        security.reload()
        return
      }
      setNotice('Письмо отправлено: проверьте почту и перейдите по ссылке')
    })

  const startTotp = () =>
    run(async () => {
      const data = await api.startTotp()
      setSetup(data)
      setRecovery(null)
      setCode('')
    })

  const enableTotp = () =>
    run(async () => {
      if (!setup) return
      const result = await api.enableTotp(code.trim())
      setRecovery(result.recovery_codes)
      setSetup(null)
      setCode('')
      security.reload()
      setNotice('Двухфакторная аутентификация включена')
    })

  const disableTotp = () =>
    run(async () => {
      await api.disableTotp(code.trim())
      setCode('')
      security.reload()
      setNotice('Двухфакторная аутентификация отключена')
    })

  const revokeSession = (id: string) =>
    run(async () => {
      await api.revokeSession(id)
      sessions.reload()
      security.reload()
      setNotice('Сессия отозвана')
    })

  const createKey = () =>
    run(async () => {
      const result = await api.createApiKey(keyName.trim() || 'Ключ', [])
      setNewKey(result.key)
      setKeyName('')
      keys.reload()
      security.reload()
    })

  const revokeKey = (id: string) =>
    run(async () => {
      await api.revokeApiKey(id)
      keys.reload()
      security.reload()
      setNotice('Ключ отозван')
    })

  if (security.loading) return <Loader rows={4} />
  if (security.error) return <ErrorBox message={security.error} />

  const data = security.data
  if (!data) return <Empty>Нет данных о безопасности аккаунта</Empty>

  return (
    <div className="stack">
      <PageHead
        title="Безопасность аккаунта"
        subtitle="Подтверждение адреса, второй фактор, активные устройства и API-ключи"
      />

      {error ? <ErrorBox message={error} /> : null}
      {notice ? <div className="notice">{notice}</div> : null}

      <div className="grid grid-4">
        <Stat
          label="Email"
          value={data.email_verified ? 'подтверждён' : 'не подтверждён'}
          hint={data.email_verified ? 'адрес проверен' : 'нажмите «Подтвердить»'}
        />
        <Stat
          label="2FA"
          value={data.mfa_enabled ? 'включена' : 'выключена'}
          hint={data.mfa_required ? 'обязательна для вашей роли' : 'TOTP-приложение'}
        />
        <Stat label="Сессии" value={data.active_sessions} hint="активные устройства" />
        <Stat label="API-ключи" value={data.active_api_keys} hint="действующие ключи" />
      </div>

      {data.warnings.length ? (
        <div className="card stack">
          <h3>Что стоит улучшить</h3>
          <ul className="list-plain">
            {data.warnings.map((warning) => (
              <li key={warning}>• {warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* ── Email ─────────────────────────────────────────────────── */}
      <div className="card stack">
        <h3>Подтверждение адреса</h3>
        {data.email_verified ? (
          <p className="muted">Адрес подтверждён. Ссылки из писем больше не нужны.</p>
        ) : (
          <>
            <p className="muted">
              Подтверждение нужно для операций с деньгами и секретами: переводы, вывод,
              регистрация узлов и MCP-серверов.
            </p>
            <div className="row">
              <button className="btn" onClick={requestVerification} disabled={busy}>
                Подтвердить адрес
              </button>
            </div>
          </>
        )}
      </div>

      {/* ── 2FA ───────────────────────────────────────────────────── */}
      <div className="card stack">
        <h3>Двухфакторная аутентификация (TOTP)</h3>
        {data.mfa_enabled ? (
          <>
            <p className="muted">
              Вход требует код из приложения (Google Authenticator, Aegis, 1Password) или
              одноразовый код восстановления.
            </p>
            <div className="row">
              <input
                className="input"
                placeholder="Текущий код"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                inputMode="numeric"
              />
              <button className="btn btn-danger" onClick={disableTotp} disabled={busy}>
                Отключить 2FA
              </button>
            </div>
          </>
        ) : setup ? (
          <>
            <p className="muted">
              Добавьте секрет в приложение (или отсканируйте ссылку) и введите код:
            </p>
            <code className="code-block">{setup.secret}</code>
            <code className="code-block small">{setup.otpauth_uri}</code>
            <div className="row">
              <input
                className="input"
                placeholder="Код из приложения"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                inputMode="numeric"
              />
              <button className="btn btn-primary" onClick={enableTotp} disabled={busy}>
                Включить
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="muted">
              Код из приложения защищает аккаунт, даже если пароль украден.
            </p>
            <button className="btn" onClick={startTotp} disabled={busy}>
              Подключить 2FA
            </button>
          </>
        )}

        {recovery ? (
          <div className="stack">
            <p className="warn-text">
              Коды восстановления показываются один раз. Сохраните их в надёжном месте:
              каждый код одноразовый.
            </p>
            <div className="grid grid-2">
              {recovery.map((item) => (
                <code key={item} className="code-block small">
                  {item}
                </code>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      {/* ── Сессии ────────────────────────────────────────────────── */}
      <div className="card stack">
        <h3>Устройства и сессии</h3>
        {sessions.loading ? <Loader rows={2} /> : null}
        {sessions.data?.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Устройство</th>
                <th>IP</th>
                <th>Активность</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sessions.data.map((item) => (
                <tr key={item.id}>
                  <td>
                    {item.user_agent ?? 'неизвестно'}
                    {item.is_current ? <span className="badge">текущая</span> : null}
                  </td>
                  <td>{item.ip_address ?? '—'}</td>
                  <td>{item.last_used_at ? relativeTime(item.last_used_at) : '—'}</td>
                  <td>
                    {item.is_current ? null : (
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => revokeSession(item.id)}
                        disabled={busy}
                      >
                        Отозвать
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>Активных сессий нет</Empty>
        )}
      </div>

      {/* ── API-ключи ─────────────────────────────────────────────── */}
      <div className="card stack">
        <h3>API-ключи</h3>
        <p className="muted">
          Ключ работает как access-токен для автоматизации. Он показывается один раз, а
          отзывается отдельно от пароля.
        </p>
        <div className="row">
          <input
            className="input"
            placeholder="Название ключа (например, CI)"
            value={keyName}
            onChange={(event) => setKeyName(event.target.value)}
          />
          <button className="btn btn-primary" onClick={createKey} disabled={busy}>
            Создать ключ
          </button>
        </div>
        {newKey ? (
          <div className="stack">
            <p className="warn-text">Ключ показывается один раз:</p>
            <code className="code-block">{newKey}</code>
          </div>
        ) : null}

        {keys.data?.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Название</th>
                <th>Префикс</th>
                <th>Создан</th>
                <th>Использован</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.data.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <code>{item.prefix}…</code>
                  </td>
                  <td>{relativeTime(item.created_at)}</td>
                  <td>{item.last_used_at ? relativeTime(item.last_used_at) : 'ни разу'}</td>
                  <td>
                    {item.revoked_at ? (
                      <span className="muted">отозван</span>
                    ) : (
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => revokeKey(item.id)}
                        disabled={busy}
                      >
                        Отозвать
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>Ключей пока нет</Empty>
        )}
      </div>
    </div>
  )
}
