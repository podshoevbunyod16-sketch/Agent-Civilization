export const statusLabels: Record<string, string> = {
  draft: 'Черновик',
  published: 'Опубликован',
  suspended: 'Приостановлен',
  archived: 'Архив',
  queued: 'В очереди',
  leased: 'Выдан узлу',
  running: 'Выполняется',
  succeeded: 'Выполнено',
  failed: 'Ошибка',
  cancelled: 'Отменено',
  expired: 'Просрочено',
  pending: 'Ожидает',
  online: 'Онлайн',
  degraded: 'Деградация',
  offline: 'Офлайн',
  revoked: 'Отозван',
  healthy: 'Здоров',
  unhealthy: 'Сбой',
  unknown: 'Неизвестно',
  cloud: 'Облачный',
  community: 'Участника',
  edge: 'Периферийный',
  http: 'HTTP',
  python: 'Python (sandbox)',
  docker: 'Docker',
  mcp: 'MCP',
  // Коммуникация агентов
  processing: 'В работе',
  delivered: 'Доставлено',
  open: 'Открыт',
  closed: 'Закрыт',
  inbox: 'Inbox',
  sandbox: 'Sandbox',
  // Планы и шаги
  planned: 'Спланирован',
  ready: 'Готов к запуску',
  skipped: 'Пропущен',
  blocked: 'Заблокирован',
  request: 'Запрос',
  response: 'Ответ',
  event: 'Событие',
  error: 'Ошибка',
}

export function label(value: string | null | undefined): string {
  if (!value) return '—'
  return statusLabels[value] ?? value
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return 'никогда'
  const diffMs = Date.now() - new Date(value).getTime()
  const minutes = Math.round(diffMs / 60000)
  if (minutes < 1) return 'только что'
  if (minutes < 60) return `${minutes} мин назад`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} ч назад`
  const days = Math.round(hours / 24)
  return `${days} дн назад`
}

export function truncate(value: string, max = 120): string {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

export function statusTone(status: string): string {
  switch (status) {
    case 'published':
    case 'online':
    case 'succeeded':
    case 'healthy':
    case 'delivered':
    case 'open':
    case 'succeeded_plan':
      return 'ok'
    case 'suspended':
    case 'degraded':
    case 'running':
    case 'leased':
    case 'processing':
    case 'ready':
    case 'blocked':
      return 'warn'
    case 'failed':
    case 'revoked':
    case 'unhealthy':
    case 'expired':
    case 'skipped':
      return 'bad'
    case 'queued':
    case 'pending':
    case 'draft':
    case 'closed':
    case 'archived':
    case 'draft':
    case 'pending':
    case 'planned':
      return 'idle'
    default:
      return 'idle'
  }
}
