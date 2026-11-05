import { Link } from 'react-router-dom'
import type { AgentListItem } from '@/api/client'
import { Reputation } from '@/components/ui'
import { label, relativeTime, statusTone } from '@/lib/format'

export default function AgentCard({ agent }: { agent: AgentListItem }) {
  const initials = agent.name
    .split(' ')
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')

  return (
    <Link
      to={`/agents/${agent.id}`}
      className="agent-card"
      style={{ ['--agent-color' as string]: agent.accent_color }}
    >
      <div className="agent-card-head">
        <div
          className="avatar"
          style={{ background: `linear-gradient(135deg, ${agent.accent_color}, #60a5fa)` }}
        >
          {initials || 'AI'}
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="agent-name">{agent.name}</div>
          <div className="agent-slug">{agent.slug}</div>
        </div>
        <span className={`badge ${statusTone(agent.status)}`}>
          <span className="dot" />
          {label(agent.status)}
        </span>
      </div>

      <div className="agent-summary">{agent.summary}</div>

      <div className="cap-list">
        {agent.capabilities.slice(0, 4).map((cap) => (
          <span
            key={cap.capability_key}
            className={`cap${cap.verified_level ? ' verified' : ''}`}
            title={
              cap.verified_level
                ? `Проверено системой: уровень ${cap.verified_level}`
                : `Заявлено разработчиком: уровень ${cap.declared_level}`
            }
          >
            {cap.capability_key}
            <span className="lvl">L{cap.verified_level ?? cap.declared_level}</span>
            {cap.verified_level ? <span title="проверено">✓</span> : null}
          </span>
        ))}
        {agent.capabilities.length > 4 ? (
          <span className="cap">+{agent.capabilities.length - 4}</span>
        ) : null}
      </div>

      <div className="card-foot">
        <Reputation value={agent.reputation_score} />
        <span>
          {agent.completed_tasks} выполнено
          {agent.failed_tasks ? ` · ${agent.failed_tasks} сбоев` : ''}
        </span>
        <span>{relativeTime(agent.published_at ?? agent.created_at)}</span>
      </div>
    </Link>
  )
}
