import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import './swarm.css'
import { Link } from 'react-router-dom'
import { api, type AgentListItem, type PlanDetail } from '@/api/client'

const DEMO_SLUGS = [
  'researcher-prime', 'polyglot-nexus', 'data-oracle', 'code-reviewer', 'task-planner',
  'demo-ux-architect', 'demo-tkinter-builder', 'demo-tkinter-reviewer',
  'demo-tkinter-test-designer', 'demo-tkinter-accessibility',
]

const SWARM_STEPS = [
  { key: 'design', name: 'Спроектировать структуру интерфейса', capability: 'ui.spec', input: (prompt: string) => ({ prompt }) },
  { key: 'build', name: 'Собрать Python + Tkinter приложение', capability: 'ui.build', input: (prompt: string) => ({ prompt }) },
  { key: 'review', name: 'Проверить сгенерированный код', capability: 'ui.review', input: (prompt: string) => ({ prompt }) },
  { key: 'tests', name: 'Подготовить тест-план', capability: 'ui.test', input: (prompt: string) => ({ prompt }) },
  { key: 'accessibility', name: 'Проверить доступность интерфейса', capability: 'ui.accessibility', input: (prompt: string) => ({ prompt }) },
]

const stateName: Record<string, string> = {
  idle: 'резерв', pending: 'ожидает шага', queued: 'в очереди', ready: 'готов', running: 'работает',
  succeeded: 'готово', failed: 'ошибка', blocked: 'заблокирован', skipped: 'пропущен',
}
const stateColor: Record<string, string> = {
  idle: '#8190a8', pending: '#8190a8', queued: '#9db6ff', ready: '#9db6ff', running: '#54d6bc',
  succeeded: '#61d994', failed: '#ff7188', blocked: '#ffb454', skipped: '#8190a8',
}

interface Props {
  agents: AgentListItem[]
  authenticated: boolean
}

export default function SwarmRebuild({ agents, authenticated }: Props) {
  const [prompt, setPrompt] = useState('Создай тёмный менеджер задач: заголовок, поле добавления и список задач с кнопкой завершения.')
  const [plan, setPlan] = useState<PlanDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [view, setView] = useState<'preview' | 'code'>('preview')

  const demoAgents = useMemo(
    () => agents.filter((agent) => DEMO_SLUGS.includes(agent.slug) && agent.status === 'published'),
    [agents],
  )
  const qualified = useMemo(
    () => new Set(demoAgents.flatMap((agent) => agent.capabilities.map((capability) => capability.capability_key))),
    [demoAgents],
  )
  const missing = SWARM_STEPS.filter((step) => !qualified.has(step.capability))
  const runningCount = plan?.steps.filter((step) => step.status === 'running').length ?? 0
  const finishedCount = plan?.steps.filter((step) => ['succeeded', 'failed', 'skipped'].includes(step.status)).length ?? 0
  const buildStep = plan?.steps.find((step) => step.key === 'build')
  const buildOutput = buildStep?.output_payload as { source_code?: string; filename?: string; title?: string; components?: string[] } | undefined
  const sourceCode = buildOutput?.source_code ?? ''
  const designStep = plan?.steps.find((step) => step.key === 'design')
  const designOutput = designStep?.output_payload as { spec?: { theme?: string; layout?: string[] } } | undefined
  const participants = plan?.steps.flatMap((step) => step.agent_id ? [step.agent_id] : []) ?? []
  const activeAgentIds = new Set(plan?.steps.filter((step) => step.status === 'running' && step.agent_id).map((step) => step.agent_id) ?? [])

  useEffect(() => {
    if (!plan?.id || ['succeeded', 'failed', 'cancelled'].includes(plan.status)) return
    const timer = window.setInterval(() => {
      void api.runPlan(plan.id).then(() => api.plan(plan.id)).then(setPlan).catch(() => undefined)
    }, 1200)
    return () => window.clearInterval(timer)
  }, [plan?.id, plan?.status])

  async function startSwarm() {
    if (!authenticated) {
      setError('Войдите в систему, чтобы создать настоящий план и задания.')
      return
    }
    if (missing.length) {
      setError(`Не найдены демо-агенты с возможностями: ${missing.map((step) => step.capability).join(', ')}. Запустите scripts/seed_demo.py.`)
      return
    }
    setBusy(true)
    setError('')
    setPlan(null)
    try {
      const steps = SWARM_STEPS.map((step) => ({
        key: step.key,
        name: step.name,
        required_capabilities: [step.capability],
        depends_on: step.key === 'design' ? [] : step.key === 'build' ? ['design'] : ['build'],
        input_payload: step.input(prompt),
      }))
      const created = await api.createPlan({
        title: `Swarm Rebuild: ${prompt.slice(0, 72)}`,
        goal: { prompt, output: 'Tkinter source code + review artifacts', mode: 'demo-template-swarm' },
        steps,
        max_parallelism: 4,
        auto_start: false,
      })
      await api.runPlan(created.id)
      setPlan(await api.plan(created.id))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось создать swarm plan')
    } finally {
      setBusy(false)
    }
  }

  function stateFor(agent: AgentListItem) {
    const step = plan?.steps.find((candidate) => candidate.agent_id === agent.id)
    if (step) return step.status
    return plan ? 'idle' : 'idle'
  }

  return <section className="swarm-card card" aria-labelledby="swarm-title">
    <div className="swarm-head">
      <div>
        <div className="eyebrow">LIVE DEMO · SWARM REBUILD</div>
        <h2 id="swarm-title">Очисти canvas — собери интерфейс роем</h2>
        <p>Запрос проходит через настоящий DAG и Python worker. Здесь до 10 помеченных DEMO AGENT; в этом сценарии участвуют пять специализированных шаблонных агентов, не подключённых к LLM.</p>
      </div>
      <div className="swarm-counter"><strong>{runningCount}</strong><span>работают сейчас</span><small>{finishedCount}/{plan?.steps.length ?? 5} шагов завершено</small></div>
    </div>

    <div className="swarm-prompt-row">
      <label className="swarm-prompt-label" htmlFor="swarm-prompt">Что построить?</label>
      <textarea id="swarm-prompt" value={prompt} maxLength={500} onChange={(event) => setPrompt(event.target.value)} placeholder="Опиши интерфейс, который нужно собрать…" />
      <div className="swarm-controls"><span>{demoAgents.length}/10 DEMO AGENT в реестре · реальные опубликованные карточки</span><button className="btn btn-primary" disabled={busy || prompt.trim().length < 8} onClick={() => void startSwarm()}>{busy ? 'Запускаем…' : '▶ Запустить swarm'}</button></div>
    </div>

    {error && <div className="alert error" role="alert">{error}{!authenticated && <> <Link to="/login">Войти →</Link></>}</div>}

    <div className="swarm-layout">
      <div className="swarm-canvas" aria-label="Рабочее поле Swarm Rebuild">
        <div className="swarm-canvas-top"><span><i /> CANVAS / TKINTER</span><span>{plan ? plan.status.toUpperCase() : 'READY'}</span></div>
        <div className="swarm-cursor-layer" aria-hidden="true">
          {demoAgents.filter((agent) => activeAgentIds.has(agent.id)).map((agent, index) => <div className="swarm-cursor" key={agent.id} style={{ '--cursor-color': agent.accent_color || '#54d6bc', '--cursor-delay': `${index * 170}ms`, '--cursor-left': `${8 + index * 13}%` } as CSSProperties}><span className="cursor-glyph">➤</span><b>{agent.name.replace('DEMO · ', '')}</b></div>)}
        </div>
        {!plan && <div className="canvas-empty"><div className="empty-cross">＋</div><b>Твой prompt → командная работа агентов</b><span>Нажми «Запустить swarm» — план и результаты появятся здесь из API.</span></div>}
        {plan && !sourceCode && <div className="canvas-workbench">
          <div className={`mock-window${plan.steps.find((step) => step.key === 'design')?.status === 'succeeded' ? ' is-visible' : ''}`}><div className="mock-window-bar"><i /><i /><i /><span>design brief</span></div><div className="mock-window-body"><span className="mock-skeleton wide" /><span className="mock-skeleton" /><span className="mock-skeleton half" /><span className="mock-skeleton wide" /></div></div>
          <div className="canvas-progress-label">{plan.steps.find((step) => step.status === 'running')?.name ?? 'Ожидаем worker…'}</div>
        </div>}
        {plan && sourceCode && <div className="canvas-built">
          <div className="canvas-preview-head"><div><b>{buildOutput?.filename ?? 'app.py'}</b><small>Сгенерированный результат Tkinter builder</small></div><div className="swarm-view-toggle"><button className={view === 'preview' ? 'selected' : ''} onClick={() => setView('preview')}>Макет</button><button className={view === 'code' ? 'selected' : ''} onClick={() => setView('code')}>Python</button></div></div>
          {view === 'code' ? <pre className="swarm-source">{sourceCode}</pre> : <div className="tk-preview"><div className="tk-titlebar"><span>● ● ●</span><b>Python · Tkinter preview</b></div><div className="tk-body"><strong>{String(buildOutput?.title ?? plan.goal?.prompt ?? 'Новое приложение').slice(0, 60)}</strong><span>{designOutput?.spec?.theme ?? 'Тёмная тема'} · {buildOutput?.components?.length ?? 0} компонента в output</span><input readOnly placeholder="Поле ввода из шаблона" /><button type="button" tabIndex={-1}>Действие из шаблона</button><small>Визуальное превью по JSON результата; Tkinter-код показан отдельно и не запускается в браузере.</small></div></div>}
        </div>}
      </div>

      <aside className="swarm-agents-panel">
        <div className="swarm-panel-title"><div><b>Команда</b><small>Статусы из реального plan</small></div><span>{participants.length ? `${new Set(participants).size} назначено` : `${demoAgents.length} доступно`}</span></div>
        <div className="swarm-agent-list">
          {demoAgents.slice(0, 10).map((agent) => {
            const state = stateFor(agent)
            const step = plan?.steps.find((candidate) => candidate.agent_id === agent.id)
            return <div className={`swarm-agent-row ${activeAgentIds.has(agent.id) ? 'is-working' : ''}`} key={agent.id}>
              <span className="swarm-agent-dot" style={{ '--state-color': stateColor[state] ?? '#8190a8' } as CSSProperties} />
              <div className="swarm-agent-copy"><b>{agent.name.replace('DEMO · ', '')}</b><small>{step?.name ?? agent.capabilities[0]?.capability_key ?? 'registered agent'}</small></div>
              <span className="swarm-agent-state" style={{ color: stateColor[state] ?? '#8190a8' }}>{stateName[state] ?? stateName.idle}</span>
              <span className="swarm-demo-badge">DEMO</span>
            </div>
          })}
          {demoAgents.length === 0 && <div className="swarm-no-agents">Запусти seed_demo.py — реальные карточки DEMO AGENT появятся в каталоге.</div>}
        </div>
        {plan && <div className="swarm-events"><b>События плана</b>{plan.events.slice(-5).map((event) => <div key={`${event.sequence}-${event.event_type}`}><span>#{event.sequence} · {event.event_type}</span><small>{event.message}</small></div>)}</div>}
      </aside>
    </div>
    <div className="swarm-honesty">API plan {plan ? plan.id.slice(0, 8) : 'не запущен'} · active = шаги со статусом <code>running</code> · шаблонные DEMO AGENT не являются LLM · sandbox политика определяется сервером.</div>
  </section>
}
