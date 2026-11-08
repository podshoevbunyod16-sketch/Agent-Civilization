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
  // Экономика
  deposit: 'Пополнение',
  hold: 'Заморозка бюджета',
  release: 'Возврат бюджета',
  payout: 'Выплата за работу',
  fee: 'Комиссия платформы',
  reward: 'Награда',
  transfer: 'Перевод',
  withdrawal: 'Вывод заработка',
  reversal: 'Отмена операции',
  adjustment: 'Корректировка',
  user: 'Счёт участника',
  agent: 'Счёт агента',
  treasury: 'Казна',
  escrow: 'Эскроу',
  held: 'Заморожен',
  settled: 'Рассчитан',
  released: 'Возвращён',
  posted: 'Проведена',
  reversed: 'Отменена',
  active: 'Активен',
  frozen: 'Заморожен',
  // Доверие и верификация
  verified: 'Подтверждён',
  unverified: 'Без проверки',
  rejected: 'Отклонён',
  disputed: 'Спорный',
  passed: 'Пройдена',
  schema: 'Схема результата',
  auto_test: 'Тест-код',
  assertions: 'Утверждения',
  peer_agent: 'Независимый агент',
  human: 'Ручная проверка',
  cross_check: 'Перекрёстная проверка',
  accepted: 'Удовлетворена',
  withdrawn: 'Отозвана',
  confirmed: 'Подтверждена',
  dismissed: 'Отклонена',
  declared: 'Заявлен',
  unconfirmed: 'Неполный',
  result_reuse: 'Повтор результата',
  rapid_results: 'Подозрительно быстро',
  flaky_output: 'Нестабильный результат',
  failure_spike: 'Всплеск отказов',
  self_dealing: 'Самопроверка',
  review_ring: 'Круг отзывов',
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
    case 'verified':
    case 'passed':
    case 'accepted':
    case 'confirmed':
      return 'ok'
    case 'declared':
    case 'unconfirmed':
    case 'disputed':
    case 'held':
      return 'warn'
    case 'settled':
      return 'ok'
    case 'reversed':
      return 'bad'
    case 'rejected':
    case 'dismissed':
    case 'unverified':
      return 'idle'
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
    case 'planned':
      return 'idle'
    default:
      return 'idle'
  }
}
