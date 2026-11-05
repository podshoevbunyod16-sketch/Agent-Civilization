import type { ReactNode } from 'react'
import { statusTone, label } from '@/lib/format'

export function Badge({ value, text }: { value: string; text?: string }) {
  return (
    <span className={`badge ${statusTone(value)}`}>
      <span className="dot" />
      {text ?? label(value)}
    </span>
  )
}

export function Stat({
  label: title,
  value,
  hint,
}: {
  label: string
  value: ReactNode
  hint?: string
}) {
  return (
    <div className="stat">
      <div className="stat-label">{title}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  )
}

export function Reputation({ value }: { value: number }) {
  const clamped = Math.max(0, Math.min(100, value))
  return (
    <span className="reputation" title={`Репутация ${clamped.toFixed(1)} / 100`}>
      <span className="reputation-bar">
        <span style={{ width: `${clamped}%` }} />
      </span>
      {clamped.toFixed(1)}
    </span>
  )
}

export function Loader({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="skeleton" style={{ height: 54 }} />
      ))}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="alert row between">
      <span>{message}</span>
      {onRetry ? (
        <button className="btn btn-sm" onClick={onRetry}>
          Повторить
        </button>
      ) : null}
    </div>
  )
}

export function PageHead({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
}) {
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {subtitle ? <p className="page-sub">{subtitle}</p> : null}
      </div>
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  )
}
