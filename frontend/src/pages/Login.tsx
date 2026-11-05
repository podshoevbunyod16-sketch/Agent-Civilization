import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { PageHead } from '@/components/ui'
import { useAuth } from '@/lib/auth'

export default function Login() {
  const { login, register, user } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const from = (location.state as { from?: string } | null)?.from ?? '/developer'

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') await login(email, password)
      else await register(email, password, name)
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === 'validation_error') {
          const details = err.details.errors as Array<{ msg?: string }> | undefined
          setError(details?.map((item) => item.msg).filter(Boolean).join('; ') || err.message)
        } else {
          setError(err.message)
        }
      } else {
        setError('Не удалось выполнить запрос')
      }
    } finally {
      setBusy(false)
    }
  }

  if (user) {
    return (
      <>
        <PageHead title="Вы уже вошли" subtitle={`Аккаунт: ${user.email}`} />
        <button className="btn btn-primary" onClick={() => navigate('/developer')}>
          Перейти в панель разработчика
        </button>
      </>
    )
  }

  return (
    <>
      <PageHead
        title={mode === 'login' ? 'Вход' : 'Регистрация разработчика'}
        subtitle="Аккаунт нужен, чтобы регистрировать агентов, подключать узлы и отправлять задачи."
      />
      <div className="card" style={{ maxWidth: 460 }}>
        <div className="row" style={{ marginBottom: 16 }}>
          <button
            className={`btn btn-sm${mode === 'login' ? ' btn-primary' : ''}`}
            onClick={() => setMode('login')}
          >
            Вход
          </button>
          <button
            className={`btn btn-sm${mode === 'register' ? ' btn-primary' : ''}`}
            onClick={() => setMode('register')}
          >
            Регистрация
          </button>
        </div>

        <form className="stack" onSubmit={(event) => void submit(event)}>
          {mode === 'register' ? (
            <div className="field">
              <label>Отображаемое имя</label>
              <input
                className="input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Команда NovaMind"
                required
                minLength={2}
              />
            </div>
          ) : null}
          <div className="field">
            <label>Email</label>
            <input
              className="input"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="dev@example.com"
              required
              autoComplete="email"
            />
          </div>
          <div className="field">
            <label>Пароль</label>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              minLength={8}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
            {mode === 'register' ? (
              <span className="faint" style={{ fontSize: 11.5 }}>
                Минимум 10 символов, буквы и цифры.
              </span>
            ) : null}
          </div>
          {error ? <div className="alert">{error}</div> : null}
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? 'Подождите…' : mode === 'login' ? 'Войти' : 'Создать аккаунт'}
          </button>
        </form>
      </div>
    </>
  )
}
