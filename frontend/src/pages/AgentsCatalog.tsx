import { useEffect, useMemo, useState } from 'react'
import { api } from '@/api/client'
import type { AgentStatus } from '@/api/client'
import AgentCard from '@/components/AgentCard'
import { Empty, ErrorBox, Loader, PageHead } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { label } from '@/lib/format'

const STATUSES: Array<AgentStatus | ''> = ['', 'published', 'suspended', 'draft', 'archived']
const SORTS = [
  { value: 'reputation', label: 'По репутации' },
  { value: 'created_at', label: 'По дате создания' },
  { value: 'name', label: 'По имени' },
  { value: 'completed_tasks', label: 'По числу задач' },
]

export default function AgentsCatalog() {
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [status, setStatus] = useState<AgentStatus | ''>('published')
  const [category, setCategory] = useState('')
  const [capability, setCapability] = useState('')
  const [sort, setSort] = useState('reputation')
  const [page, setPage] = useState(1)
  const [verifiedOnly, setVerifiedOnly] = useState(false)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 350)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => setPage(1), [debounced, status, category, capability, sort, verifiedOnly])

  const capabilities = useAsync((signal) => api.capabilities(signal), [])

  const result = useAsync(
    (signal) =>
      api.agents(
        {
          q: debounced || undefined,
          status: status || undefined,
          category: category || undefined,
          capability: capability || undefined,
          verified_only: verifiedOnly || undefined,
          sort,
          order: sort === 'name' ? 'asc' : 'desc',
          page,
          page_size: 12,
        },
        signal,
      ),
    [debounced, status, category, capability, sort, page, verifiedOnly],
  )

  const categories = useMemo(() => {
    const items = result.data?.items ?? []
    return Array.from(new Set(items.map((agent) => agent.category))).sort()
  }, [result.data])

  return (
    <>
      <PageHead
        title="Каталог агентов"
        subtitle="Реестр зарегистрированных агентов с заявленными и проверенными возможностями, статусом и репутацией."
      />

      <div className="toolbar">
        <div className="field grow">
          <label>Поиск</label>
          <input
            className="input"
            placeholder="Название, описание, slug…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="field">
          <label>Статус</label>
          <select
            className="select"
            value={status}
            onChange={(event) => setStatus(event.target.value as AgentStatus | '')}
          >
            {STATUSES.map((value) => (
              <option key={value || 'all'} value={value}>
                {value ? label(value) : 'Все доступные мне'}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Возможность</label>
          <select
            className="select"
            value={capability}
            onChange={(event) => setCapability(event.target.value)}
          >
            <option value="">Любая</option>
            {(capabilities.data ?? []).map((cap) => (
              <option key={cap.key} value={cap.key}>
                {cap.key}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Сортировка</label>
          <select className="select" value={sort} onChange={(event) => setSort(event.target.value)}>
            {SORTS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <label className="row" style={{ fontSize: 13, gap: 8, paddingBottom: 10 }}>
          <input
            type="checkbox"
            checked={verifiedOnly}
            onChange={(event) => setVerifiedOnly(event.target.checked)}
          />
          Только верифицированные
        </label>
      </div>

      {categories.length > 1 ? (
        <div className="row" style={{ marginBottom: 16 }}>
          <span className="faint" style={{ fontSize: 12 }}>
            Категории на странице:
          </span>
          {categories.map((item) => (
            <button
              key={item}
              className="btn btn-sm"
              onClick={() => setCategory(category === item ? '' : item)}
              style={category === item ? { borderColor: 'rgba(110,231,183,0.6)' } : undefined}
            >
              {item}
            </button>
          ))}
          {category ? (
            <button className="btn btn-sm" onClick={() => setCategory('')}>
              Сбросить
            </button>
          ) : null}
        </div>
      ) : null}

      {result.loading ? (
        <Loader rows={4} />
      ) : result.error ? (
        <ErrorBox message={result.error} onRetry={result.reload} />
      ) : result.data && result.data.items.length > 0 ? (
        <>
          <div className="grid grid-cards">
            {result.data.items.map((agent) => (
              <AgentCard key={agent.id} agent={agent} />
            ))}
          </div>
          <div className="row between" style={{ marginTop: 20 }}>
            <span className="faint" style={{ fontSize: 12.5 }}>
              Найдено {result.data.meta.total} · страница {result.data.meta.page} из{' '}
              {result.data.meta.pages}
            </span>
            <div className="row">
              <button
                className="btn btn-sm"
                disabled={page <= 1}
                onClick={() => setPage((value) => Math.max(1, value - 1))}
              >
                ← Назад
              </button>
              <button
                className="btn btn-sm"
                disabled={page >= result.data.meta.pages}
                onClick={() => setPage((value) => value + 1)}
              >
                Вперёд →
              </button>
            </div>
          </div>
        </>
      ) : (
        <Empty>
          <p>Агенты не найдены.</p>
          <p className="faint" style={{ fontSize: 12.5 }}>
            Измените фильтры или создайте первого агента в панели разработчика.
          </p>
        </Empty>
      )}
    </>
  )
}
