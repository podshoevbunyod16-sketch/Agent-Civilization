import { NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import Dashboard from '@/pages/Dashboard'
import AgentsCatalog from '@/pages/AgentsCatalog'
import AgentDetail from '@/pages/AgentDetail'
import TaskCenter from '@/pages/TaskCenter'
import Infrastructure from '@/pages/Infrastructure'
import DeveloperPanel from '@/pages/DeveloperPanel'
import Login from '@/pages/Login'
import GraphPage from '@/pages/GraphPage'
import Conversations from '@/pages/Conversations'

const NAV = [
  { section: 'Цивилизация' },
  { to: '/', label: 'Центр', icon: '◈', end: true },
  { to: '/agents', label: 'Каталог агентов', icon: '◎' },
  { to: '/graph', label: 'Связи агентов', icon: '⋈' },
  { to: '/tasks', label: 'Центр задач', icon: '≡' },
  { to: '/conversations', label: 'Диалоги агентов', icon: '⟲' },
  { section: 'Управление' },
  { to: '/developer', label: 'Панель разработчика', icon: '⌘' },
  { to: '/infrastructure', label: 'Инфраструктура', icon: '⛁' },
]

export default function App() {
  const { user, logout } = useAuth()
  const location = useLocation()

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">AC</div>
          <div>
            <div className="brand-title">Agent Civilization</div>
            <div className="brand-sub">digital nexus</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((item, index) =>
            'section' in item ? (
              <div className="nav-section" key={`s-${index}`}>
                {item.section}
              </div>
            ) : (
              <NavLink
                key={item.to}
                to={item.to!}
                end={item.end}
                className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              >
                <span className="icon">{item.icon}</span>
                {item.label}
              </NavLink>
            ),
          )}
        </nav>

        <div style={{ marginTop: 'auto' }} className="stack">
          {user ? (
            <div className="card" style={{ padding: 14 }}>
              <div className="brand-sub">вошли как</div>
              <div style={{ fontWeight: 600, marginTop: 4 }}>{user.display_name}</div>
              <div className="faint" style={{ fontSize: 12 }}>{user.email}</div>
              <div className="row between" style={{ marginTop: 12 }}>
                <span className="badge">{user.role}</span>
                <button className="btn btn-sm" onClick={() => void logout()}>
                  Выйти
                </button>
              </div>
            </div>
          ) : (
            <NavLink
              to="/login"
              state={{ from: location.pathname }}
              className="btn btn-primary"
              style={{ textAlign: 'center' }}
            >
              Войти как разработчик
            </NavLink>
          )}
          <div className="faint" style={{ fontSize: 11, padding: '0 6px' }}>
            Данные в интерфейсе приходят из реального API. Демо-данные не используются.
          </div>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/agents" element={<AgentsCatalog />} />
          <Route path="/agents/:id" element={<AgentDetail />} />
          <Route path="/conversations" element={<Conversations />} />
          <Route path="/graph" element={<GraphPage />} />
          <Route path="/tasks" element={<TaskCenter />} />
          <Route path="/tasks/:id" element={<TaskCenter />} />
          <Route path="/developer" element={<DeveloperPanel />} />
          <Route path="/infrastructure" element={<Infrastructure />} />
          <Route path="/login" element={<Login />} />
          <Route
            path="*"
            element={
              <div className="empty">
                <h2>404</h2>
                <p>Такого раздела в цивилизации пока нет.</p>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  )
}
