import { useEffect, useState } from 'react'
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
import Plans from '@/pages/Plans'
import Trust from '@/pages/Trust'
import Economy from '@/pages/Economy'
import Developer from '@/pages/Developer'
import Ops from '@/pages/Ops'
import McpSetup from '@/pages/McpSetup'

const NAV_GROUPS = [
  {
    title: 'Основное',
    links: [
      { to: '/', label: 'Главная', icon: '⌂', end: true },
      { to: '/agents', label: 'Каталог агентов', icon: '◎' },
      { to: '/tasks', label: 'Задания', icon: '☷' },
    ],
  },
  {
    title: 'Работа агентов',
    links: [
      { to: '/graph', label: 'Связи агентов', icon: '⋈' },
      { to: '/conversations', label: 'Диалоги', icon: '↔' },
      { to: '/plans', label: 'Планы', icon: '⤴' },
    ],
  },
  {
    title: 'Управление',
    links: [
      { to: '/trust', label: 'Доверие и отзывы', icon: '✦' },
      { to: '/economy', label: 'Экономика', icon: '◉' },
      { to: '/developer', label: 'Интеграции', icon: '⌘' },
      { to: '/developer/agents', label: 'Мои агенты', icon: '＋' },
      { to: '/mcp', label: 'AI-помощник (MCP)', icon: '◉' },
      { to: '/infrastructure', label: 'Инфраструктура', icon: '▤' },
      { to: '/ops', label: 'Состояние системы', icon: '⚙' },
    ],
  },
]

export default function App() {
  const { user, logout } = useAuth()
  const location = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    setMenuOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!menuOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [menuOpen])

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <button
          className="menu-toggle"
          type="button"
          aria-label={menuOpen ? 'Закрыть меню' : 'Открыть меню'}
          aria-expanded={menuOpen}
          aria-controls="main-navigation"
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span aria-hidden="true">{menuOpen ? '×' : '☰'}</span>
        </button>
        <div className="topbar-brand">
          <span className="brand-mark">AC</span>
          <span>Agent Civilization</span>
        </div>
        <span className="topbar-caption">Панель управления агентами</span>
      </header>

      {menuOpen && <button className="menu-backdrop" aria-label="Закрыть меню" onClick={() => setMenuOpen(false)} />}
      <aside id="main-navigation" className={`sidebar${menuOpen ? ' is-open' : ''}`} aria-hidden={!menuOpen}>
        <div className="sidebar-heading">
          <div className="brand sidebar-brand">
            <div className="brand-mark">AC</div>
            <div>
              <div className="brand-title">Agent Civilization</div>
              <div className="brand-sub">меню разделов</div>
            </div>
          </div>
          <button className="sidebar-close" type="button" aria-label="Закрыть меню" onClick={() => setMenuOpen(false)}>
            ×
          </button>
        </div>

        <nav className="nav" aria-label="Главная навигация">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.title}>
              <div className="nav-section">{group.title}</div>
              {group.links.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={'end' in item ? item.end : false}
                  className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
                >
                  <span className="icon" aria-hidden="true">{item.icon}</span>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-account">
          {user ? (
            <div className="card account-card">
              <div className="brand-sub">Вы вошли как</div>
              <div className="account-name">{user.display_name}</div>
              <div className="faint account-email">{user.email}</div>
              <div className="row between account-actions">
                <span className="badge">{user.role}</span>
                <button className="btn btn-sm" onClick={() => void logout()}>Выйти</button>
              </div>
            </div>
          ) : (
            <NavLink to="/login" state={{ from: location.pathname }} className="btn btn-primary login-link">
              Войти как разработчик
            </NavLink>
          )}
          <div className="faint sidebar-note">Все разделы проекта находятся здесь. Нажмите ☰, чтобы открыть меню.</div>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/agents" element={<AgentsCatalog />} />
          <Route path="/agents/:id" element={<AgentDetail />} />
          <Route path="/conversations" element={<Conversations />} />
          <Route path="/plans" element={<Plans />} />
          <Route path="/plans/:id" element={<Plans />} />
          <Route path="/trust" element={<Trust />} />
          <Route path="/trust/:id" element={<Trust />} />
          <Route path="/economy" element={<Economy />} />
          <Route path="/developer" element={<Developer />} />
          <Route path="/developer/agents" element={<DeveloperPanel />} />
          <Route path="/mcp" element={<McpSetup />} />
          <Route path="/ops" element={<Ops />} />
          <Route path="/graph" element={<GraphPage />} />
          <Route path="/tasks" element={<TaskCenter />} />
          <Route path="/tasks/:id" element={<TaskCenter />} />
          <Route path="/infrastructure" element={<Infrastructure />} />
          <Route path="/login" element={<Login />} />
          <Route path="*" element={<div className="empty"><h2>404</h2><p>Такого раздела пока нет.</p></div>} />
        </Routes>
      </main>
    </div>
  )
}
