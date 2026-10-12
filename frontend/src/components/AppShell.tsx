import { Link, useLocation } from 'react-router'
import { useEffect, useRef } from 'react'
import { useAuth } from '../auth/auth'

type AppShellProps = {
  title: string
  eyebrow?: string
  children: React.ReactNode
  wide?: boolean
}

export default function AppShell({ title, eyebrow, children, wide = false }: AppShellProps) {
  const { user, logout } = useAuth()
  const heading = useRef<HTMLHeadingElement>(null)
  const location = useLocation()

  useEffect(() => {
    heading.current?.focus()
  }, [location.pathname])

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__content">
          <Link className="brand" to={user ? '/jobs' : '/login'} aria-label="AI Interviewer home">
            <span className="brand__mark" aria-hidden="true">◎</span>
            <span>AI Interviewer</span>
          </Link>
          {user && (
            <div className="topbar__account">
              <span className="account-email">{user.email}</span>
              <button type="button" className="button button--quiet" onClick={logout}>Log out</button>
            </div>
          )}
        </div>
      </header>
      <main className={`page ${wide ? 'page--wide' : ''}`}>
        <header className="page__intro">
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h1 tabIndex={-1} ref={heading}>{title}</h1>
        </header>
        {children}
      </main>
    </div>
  )
}
