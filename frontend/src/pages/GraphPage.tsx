import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '@/api/client'
import { Empty, ErrorBox, Loader, PageHead } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'

/**
 * Визуализация связей между агентами.
 *
 * Связь строится по общим возможностям: два агента соединены, если у них
 * есть хотя бы одна одинаковая capability. Это реальные данные реестра,
 * а не иллюстрация: граф пересобирается из опубликованных агентов.
 */
export default function GraphPage() {
  const [hovered, setHovered] = useState<string | null>(null)
  const agents = useAsync(
    (signal) => api.agents({ status: 'published', page_size: 30, sort: 'reputation' }, signal),
    [],
  )

  const graph = useMemo(() => {
    const items = agents.data?.items ?? []
    const width = 760
    const height = 460
    const centerX = width / 2
    const centerY = height / 2
    const radius = Math.min(width, height) / 2 - 70

    const nodes = items.map((agent, index) => {
      const angle = (index / Math.max(1, items.length)) * Math.PI * 2 - Math.PI / 2
      // Агенты с высокой репутацией — ближе к центру
      const ring = radius * (1 - Math.min(1, agent.reputation_score / 140))
      return {
        ...agent,
        x: centerX + Math.cos(angle) * ring,
        y: centerY + Math.sin(angle) * ring * 0.86,
      }
    })

    const edges: Array<{ a: string; b: string; key: string; shared: string[] }> = []
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const keysA = new Set(nodes[i].capabilities.map((cap) => cap.capability_key))
        const shared = nodes[j].capabilities
          .map((cap) => cap.capability_key)
          .filter((key) => keysA.has(key))
        if (shared.length > 0) {
          edges.push({ a: nodes[i].id, b: nodes[j].id, key: `${nodes[i].id}-${nodes[j].id}`, shared })
        }
      }
    }
    return { nodes, edges, width, height, centerX, centerY }
  }, [agents.data])

  const positions = new Map(graph.nodes.map((node) => [node.id, node]))

  return (
    <>
      <PageHead
        title="Связи агентов"
        subtitle="Граф совместных возможностей: агенты соединены, если могут выполнять одну и ту же функцию. Размер узла — репутация, цвет — категория."
      />

      {agents.loading ? (
        <Loader rows={4} />
      ) : agents.error ? (
        <ErrorBox message={agents.error} onRetry={agents.reload} />
      ) : graph.nodes.length === 0 ? (
        <Empty>
          <p>Недостаточно опубликованных агентов для построения графа.</p>
          <p className="faint" style={{ fontSize: 12.5 }}>
            Опубликуйте минимум двух агентов с общими возможностями.
          </p>
        </Empty>
      ) : (
        <div className="two-col">
          <div className="card" style={{ padding: 8 }}>
            <svg className="graph" viewBox={`0 0 ${graph.width} ${graph.height}`} role="img">
              <defs>
                <radialGradient id="core" cx="50%" cy="50%">
                  <stop offset="0%" stopColor="rgba(110,231,183,0.22)" />
                  <stop offset="100%" stopColor="rgba(110,231,183,0)" />
                </radialGradient>
              </defs>
              <circle cx={graph.centerX} cy={graph.centerY} r={90} fill="url(#core)" />
              {graph.edges.map((edge) => {
                const a = positions.get(edge.a)
                const b = positions.get(edge.b)
                if (!a || !b) return null
                const active = hovered === edge.a || hovered === edge.b
                return (
                  <line
                    key={edge.key}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={active ? 'rgba(110,231,183,0.8)' : 'rgba(96,165,250,0.22)'}
                    strokeWidth={active ? 1.8 : 1}
                  />
                )
              })}
              {graph.nodes.map((node) => {
                const size = 8 + (node.reputation_score / 100) * 12
                const active = hovered === node.id
                return (
                  <g
                    key={node.id}
                    onMouseEnter={() => setHovered(node.id)}
                    onMouseLeave={() => setHovered(null)}
                    style={{ cursor: 'pointer' }}
                  >
                    <circle
                      cx={node.x}
                      cy={node.y}
                      r={size + (active ? 3 : 0)}
                      fill={node.accent_color}
                      fillOpacity={active ? 0.95 : 0.72}
                      stroke="rgba(255,255,255,0.25)"
                    />
                    <text
                      className="graph-node-label"
                      x={node.x}
                      y={node.y + size + 12}
                      textAnchor="middle"
                      fill={active ? '#e8eefc' : '#95a5c0'}
                    >
                      {node.name.length > 18 ? `${node.name.slice(0, 17)}…` : node.name}
                    </text>
                  </g>
                )
              })}
            </svg>
          </div>

          <aside className="stack">
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Показатели графа</h2>
              </div>
              <dl className="kv">
                <dt>Агентов</dt>
                <dd>{graph.nodes.length}</dd>
                <dt>Связей</dt>
                <dd>{graph.edges.length}</dd>
                <dt>Категорий</dt>
                <dd>{new Set(graph.nodes.map((node) => node.category)).size}</dd>
                <dt>Плотность</dt>
                <dd>
                  {graph.nodes.length > 1
                    ? (graph.edges.length / ((graph.nodes.length * (graph.nodes.length - 1)) / 2)).toFixed(3)
                    : '0'}
                </dd>
              </dl>
            </div>

            <div className="card">
              <div className="card-head">
                <h2 className="card-title">{hovered ? 'Выбранный агент' : 'Наведите на узел'}</h2>
              </div>
              {hovered && positions.get(hovered) ? (
                (() => {
                  const node = positions.get(hovered)!
                  const shared = graph.edges
                    .filter((edge) => edge.a === node.id || edge.b === node.id)
                    .flatMap((edge) => edge.shared)
                  return (
                    <div className="stack" style={{ gap: 8 }}>
                      <Link to={`/agents/${node.id}`} style={{ fontWeight: 600 }}>
                        {node.name}
                      </Link>
                      <div className="faint" style={{ fontSize: 12 }}>
                        {node.category} · репутация {node.reputation_score.toFixed(1)}
                      </div>
                      <div className="row">
                        {Array.from(new Set(shared)).map((key) => (
                          <span key={key} className="tag">
                            {key}
                          </span>
                        ))}
                      </div>
                    </div>
                  )
                })()
              ) : (
                <div className="faint" style={{ fontSize: 13 }}>
                  Узлы связаны общими возможностями. Чем больше связей, тем выше взаимозаменяемость
                  агентов и устойчивость цивилизации к отказу отдельного участника.
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  )
}
