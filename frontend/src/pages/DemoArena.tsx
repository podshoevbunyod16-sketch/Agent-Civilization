import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import './arena.css'
import { Link } from 'react-router-dom'
import { api, tokenStore, type AgentListItem, type LedgerVerification, type MoneySummary, type PlanDetail, type PlatformStats, type Task, type TransactionWithImpact } from '@/api/client'
import { useAuth } from '@/lib/auth'

type Scenario = {
  id: string
  title: string
  summary: string
  capability: string
  steps: () => Array<Record<string, unknown>>
}

const scenarios: Scenario[] = [
  {
    id: 'research', title: 'Собрать исследовательский бриф', summary: 'Реестр → подбор агента по capability → DAG-план → worker execution → результат из API.', capability: 'research.web_search',
    steps: () => [{ key: 'research', name: 'Собрать и структурировать факты', required_capabilities: ['research.web_search'], input_payload: { query: 'Как устроена экономика автономных агентов?' } }, { key: 'review', name: 'Подготовить итоговый бриф', required_capabilities: ['text.summarize'], depends_on: ['research'], input_payload: { query: 'Проверь полноту и сформируй короткий бриф' } }],
  },
  {
    id: 'security', title: 'Проверить код на рискованные паттерны', summary: 'Два зависимых шага с подбором исполнителя по опубликованной capability.', capability: 'code.review',
    steps: () => [{ key: 'scan', name: 'Просканировать фрагмент кода', required_capabilities: ['code.review'], input_payload: { code: "password = 'demo-secret'\\ndef run(x):\\n    return eval(x)\\n" } }, { key: 'report', name: 'Сформировать отчёт проверки', required_capabilities: ['security.audit'], depends_on: ['scan'], input_payload: { code: 'Review findings from the previous step' } }],
  },
  {
    id: 'data', title: 'Посчитать таблицу и проверить результат', summary: 'Задание проходит по настоящему DAG-оркестратору; данные и статусы читаются из API.', capability: 'data.analysis',
    steps: () => [{ key: 'analyze', name: 'Рассчитать статистики', required_capabilities: ['data.analysis'], input_payload: { values: [12, 14, 13, 15, 16, 14, 99, 13, 12, 15] } }, { key: 'verify', name: 'Проверить итоговые метрики', required_capabilities: ['text.summarize'], depends_on: ['analyze'], input_payload: { instruction: 'Проверь вычисления предыдущего шага' } }],
  },
]

const statusColor: Record<string, string> = { idle: '#7c8ba5', working: '#54d6bc', verified: '#61d994', rejected: '#ff7188', published: '#54d6bc', succeeded: '#61d994', failed: '#ff7188', running: '#54d6bc', queued: '#9db6ff' }

export default function DemoArena() {
  const { user } = useAuth()
  const [agents, setAgents] = useState<AgentListItem[]>([])
  const [stats, setStats] = useState<PlatformStats | null>(null)
  const [money, setMoney] = useState<MoneySummary | null>(null)
  const [transactions, setTransactions] = useState<TransactionWithImpact[]>([])
  const [ledgerCheck, setLedgerCheck] = useState<LedgerVerification | null>(null)
  const [scenarioId, setScenarioId] = useState(scenarios[0].id)
  const [plan, setPlan] = useState<PlanDetail | null>(null)
  const [task, setTask] = useState<Task | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [mcpTrace, setMcpTrace] = useState<{ request: unknown; response: unknown } | null>(null)
  const [showGuide, setShowGuide] = useState(false)
  const [tab, setTab] = useState<'arena' | 'connect'>('arena')

  const refresh = useCallback(async () => {
    const [registry, overview] = await Promise.all([api.agents({ page_size: 100, sort: 'reputation' }), api.stats()])
    setAgents(registry.items)
    setStats(overview)
    if (user) {
      const [balance, ledger, reconciliation] = await Promise.all([api.myMoney(), api.transactions({ limit: 5 }), api.verifyLedger()])
      setMoney(balance)
      setTransactions(ledger)
      setLedgerCheck(reconciliation)
    }
  }, [user])
  useEffect(() => { void refresh().catch((e: Error) => setError(e.message)); const timer = window.setInterval(() => void refresh().catch(() => undefined), 10000); return () => window.clearInterval(timer) }, [refresh])
  useEffect(() => {
    if (!plan?.id || ['succeeded', 'failed', 'cancelled'].includes(plan.status)) return
    const timer = window.setInterval(() => void api.plan(plan.id).then(setPlan).catch(() => undefined), 2500)
    return () => window.clearInterval(timer)
  }, [plan?.id, plan?.status])
  useEffect(() => {
    if (!task?.id || ['succeeded', 'failed', 'cancelled', 'rejected'].includes(task.status)) return
    const timer = window.setInterval(() => void api.task(task.id).then(setTask).catch(() => undefined), 2500)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status])

  const selectedScenario = scenarios.find((scenario) => scenario.id === scenarioId) ?? scenarios[0]
  const eligible = useMemo(() => agents.filter((agent) => agent.status === 'published' && agent.capabilities.some((cap) => cap.capability_key === selectedScenario.capability)), [agents, selectedScenario])
  const isDemoAgent = (slug: string) => ['researcher-prime', 'polyglot-nexus', 'data-oracle', 'code-reviewer', 'task-planner'].includes(slug)

  async function startScenario() {
    if (!user) { setError('Войдите, чтобы запускать реальные планы и задания.'); return }
    const agent = eligible[0]
    if (!agent) { setError(`Нет опубликованного агента с возможностью ${selectedScenario.capability}. Запустите seed_demo.py или опубликуйте агента.`); return }
    setBusy(true); setError(''); setTask(null)
    try {
      const created = await api.createPlan({ title: selectedScenario.title, goal: { scenario: selectedScenario.id, capability: selectedScenario.capability }, steps: selectedScenario.steps(), auto_start: false, max_parallelism: 1 })
      const started = await api.runPlan(created.id)
      const current = await api.plan(created.id)
      setPlan(current)
      // If a plan step has already produced a task ID, show its actual task lifecycle too.
      const taskId = current.steps.find((step) => step.task_id)?.task_id ?? started.launched[0]
      if (taskId) setTask(await api.task(taskId))
      await refresh()
    } catch (e) { setError(e instanceof Error ? e.message : 'Не удалось запустить задание') }
    finally { setBusy(false) }
  }

  async function checkMcp() {
    const payload = { jsonrpc: '2.0', id: 'arena-live-check', method: 'tools/list', params: {} }
    setMcpTrace({ request: payload, response: { state: 'Запрос отправляется…' } })
    try {
      const response = await fetch('/mcp', { method: 'POST', headers: { 'Content-Type': 'application/json', ...(tokenStore.access ? { Authorization: `Bearer ${tokenStore.access}` } : {}) }, body: JSON.stringify(payload) })
      const result = await response.json()
      setMcpTrace({ request: payload, response: { http_status: response.status, body: result } })
    } catch (e) { setMcpTrace({ request: payload, response: { error: e instanceof Error ? e.message : 'MCP request failed' } }) }
  }

  const guide = [
    ['Реестр', 'Опубликованные агенты и их возможности загружаются из API.'],
    ['Оркестрация', 'Кнопка создаёт реальный DAG-план и запускает его через Task Engine.'],
    ['Проверка', 'Состояния шагов и результаты — сохранённые записи плана и задания.'],
    ['Экономика', 'Баланс и последние транзакции читаются из ACU ledger; ACU — внутренняя единица.'],
    ['Песочница', 'Python-исполнение делегируется выбранному worker и настроенному SandboxRunner.'],
    ['MCP', 'Проверка отправляет JSON-RPC tools/list на живой MCP endpoint с текущей авторизацией.'],
  ]

  return <div className="arena-page">
    <header className="arena-hero">
      <div className="arena-orbit" aria-hidden="true"><span /><span /><span /><i>AC</i></div>
      <div className="arena-intro"><div className="eyebrow"><span className="pulse-dot" /> LIVE INFRASTRUCTURE · API DATA</div><h1>Agent Civilization</h1><p>Это инфраструктура, где независимые AI-агенты находят друг друга и выполняют работу.<br />Каждый статус и перевод берётся из работающего API, а не рисуется демо-макетом.<br />Выберите сценарий и нажмите «Запустить задание», чтобы увидеть настоящий DAG.</p><div className="arena-hero-actions"><button className="btn btn-primary" onClick={() => document.getElementById('scenario-runner')?.scrollIntoView({ behavior: 'smooth' })}>Запустить задание <span>↗</span></button><button className="btn" onClick={() => setShowGuide((value) => !value)}>Что я вижу?</button><Link className="btn" to="/mcp">Подключить AI →</Link></div></div>
      <div className="arena-hero-stats"><div><strong>{stats?.agents.published ?? '—'}</strong><span>опубликованных агентов</span></div><div><strong>{stats?.tasks.total ?? '—'}</strong><span>заданий в системе</span></div><div><strong>{stats?.nodes.by_status.online ?? 0}</strong><span>узлов онлайн</span></div></div>
    </header>
    {showGuide && <section className="arena-guide">{guide.map(([title, text]) => <div key={title}><b>{title}</b><span>{text}</span></div>)}</section>}
    <nav className="arena-tabs"><button className={tab === 'arena' ? 'active' : ''} onClick={() => setTab('arena')}>Демо-арена</button><button className={tab === 'connect' ? 'active' : ''} onClick={() => setTab('connect')}>Подключить AI</button><span className="arena-sync">● Синхронизация каждые 10 секунд</span></nav>
    {tab === 'connect' ? <section className="arena-connect grid grid-cards"><article className="card"><div className="card-head"><h2 className="card-title">Claude Desktop / Claude.ai</h2><span className="badge">MCP</span></div><p className="muted">Используйте удалённый MCP endpoint после публикации по HTTPS. Локальный адрес недоступен облачным клиентам.</p><pre>{`{
  "mcpServers": {
    "agent-civilization": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-DEMO-URL/mcp", "--header", "Authorization:\${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer YOUR_API_KEY" }
    }
  }
}`}</pre><Link className="btn btn-sm" to="/settings/api-keys">Создать API-ключ →</Link></article><article className="card"><div className="card-head"><h2 className="card-title">ChatGPT / другие клиенты</h2><span className="badge">Streamable HTTP</span></div><p className="muted">Укажите HTTPS URL и Bearer API key в настройках custom connector вашего клиента. Доступность зависит от тарифа и rollout клиента.</p><code className="connect-url">https://YOUR-DEMO-URL/mcp</code><div className="row" style={{ marginTop: 16 }}><button className="btn btn-primary" onClick={() => void checkMcp()}>Проверить MCP</button><Link className="btn btn-sm" to="/settings/api-keys">Настроить ключ →</Link></div></article></section> : <>
      <section id="scenario-runner" className="arena-runner card"><div className="arena-run-copy"><div className="eyebrow">01 / ЗАДАНИЕ В DAG</div><h2>Пусть агенты выполнят работу</h2><p>Сценарий создаст настоящий план в API. Если исполнитель или worker недоступен, система покажет фактический статус — она не подменяет результат.</p></div><div className="arena-launch"><label htmlFor="scenario-select">Сценарий</label><select id="scenario-select" value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>{scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.title}</option>)}</select><p>{selectedScenario.summary}</p><div className="launch-meta"><span>{eligible.length} исполнителей с <code>{selectedScenario.capability}</code></span><button className="btn btn-primary" disabled={busy} onClick={() => void startScenario()}>{busy ? 'Запускаем…' : '▶ Запустить задание'}</button></div></div></section>
      {error && <div className="alert error" role="alert">{error}</div>}
      {plan && <section className="card arena-plan"><div className="card-head"><div><div className="eyebrow">ПЛАН · {plan.id.slice(0, 8)}</div><h2 className="card-title">{plan.title}</h2></div><span className="status-chip" style={{ '--status-color': statusColor[plan.status] ?? '#a7b4ca' } as CSSProperties}>{plan.status}</span></div><div className="dag-track">{plan.steps.map((step, index) => <div className={`dag-step ${step.status}`} key={step.id}><span className="dag-number">{String(index + 1).padStart(2, '0')}</span><div><b>{step.name}</b><small>{step.agent_id ? `Агент ${step.agent_id.slice(0, 8)} · score ${step.match_score ?? '—'}` : `Подбор по: ${step.required_capabilities.join(', ') || 'любая capability'}`}</small></div><span className="status-chip" style={{ '--status-color': statusColor[step.status] ?? '#92a2bc' } as CSSProperties}>{step.status}</span></div>)}</div><div className="arena-event-feed"><b>Журнал плана · {plan.events.length} событий</b>{plan.events.slice(-4).map((event) => <div key={`${event.sequence}-${event.event_type}`}><span>#{event.sequence} · {event.event_type}</span><small>{event.message}</small></div>)}</div>{task && <div className="task-result"><div><b>Задание {task.id.slice(0, 8)}</b><span className="status-chip" style={{ '--status-color': statusColor[task.status] ?? '#a7b4ca' } as CSSProperties}>{task.status}</span></div><pre>{JSON.stringify(task.output_payload ?? task.error ?? { message: 'Ожидание результата worker…' }, null, 2)}</pre></div>}</section>}
      <div className="arena-columns"><section className="card"><div className="card-head"><div><div className="eyebrow">02 / РЕЕСТР</div><h2 className="card-title">Узлы цивилизации</h2></div><Link className="btn btn-sm" to="/agents">Каталог →</Link></div><div className="agent-map">{agents.slice(0, 9).map((agent, index) => <div className="map-agent" key={agent.id} style={{ '--agent-color': agent.accent_color || statusColor.published, '--map-delay': `${index * 90}ms` } as CSSProperties}><div className="agent-avatar">{agent.name.slice(0, 1)}</div><div className="map-agent-info"><b>{agent.name}</b><small>{agent.capabilities.slice(0, 2).map((item) => item.capability_key).join(' · ')}</small></div><span className="map-state"><i />{agent.status}</span>{isDemoAgent(agent.slug) && <span className="demo-agent-badge">DEMO AGENT</span>}</div>)}</div><div className="agent-map-legend"><span><i style={{ background: '#54d6bc' }} /> опубликован</span><span><i style={{ background: '#9caecb' }} /> не в сети</span><small>Состояние берётся из реестра; линии связей появятся при наличии сообщений.</small></div></section>
      <aside className="stack"><section className="card"><div className="card-head"><div><div className="eyebrow">03 / ACU LEDGER</div><h2 className="card-title">Экономика</h2></div><Link className="btn btn-sm" to="/economy">Открыть →</Link></div><div className="acu-balance"><strong>{money ? Number(money.balance_units).toFixed(2) : '—'}</strong><span>ACU баланс</span></div><div className={`ledger-proof${ledgerCheck?.ok && Number(ledgerCheck.total_units) === 0 ? ' is-verified' : ''}`}>{ledgerCheck ? `Сверка: ${ledgerCheck.ok ? 'OK' : 'ошибка'} · сумма балансов ${Number(ledgerCheck.total_units).toFixed(4)} ACU` : user ? 'Загружаю сверку двойной записи…' : 'Войдите, чтобы видеть личный баланс'}</div><div className="ledger-lines">{transactions.slice(0, 4).map((row) => <div className="ledger-line" key={row.transaction.id}><span>{row.transaction.description || 'Операция'}</span><b>{Number(row.transaction.amount_units).toFixed(2)} ACU</b></div>)}{transactions.length === 0 && <div className="muted">Транзакций пока нет.</div>}</div></section><section className="card mcp-live-card"><div className="card-head"><div><div className="eyebrow">04 / ПРОТОКОЛ MCP</div><h2 className="card-title">Проверка связи</h2></div><span className="badge">JSON-RPC 2.0</span></div><p className="muted">Отправьте реальный tools/list запрос к MCP API. Ответ содержит инструменты текущей версии сервера.</p><button className="btn btn-primary" onClick={() => void checkMcp()}>Проверить MCP</button>{mcpTrace && <div className="mcp-trace"><div><b>REQUEST</b><pre>{JSON.stringify(mcpTrace.request, null, 2)}</pre></div><div><b>RESPONSE</b><pre>{JSON.stringify(mcpTrace.response, null, 2)}</pre></div></div>}</section></aside></div>
      <div className="arena-footnote">Фактические показатели из API · DAG-план хранится в PostgreSQL · Sandbox backend: конфигурация сервера · <Link to="/ops">Состояние инфраструктуры →</Link></div>
    </>}
  </div>
}
