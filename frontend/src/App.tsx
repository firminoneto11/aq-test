import { useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router'
import { z } from 'zod'
import { readApiBaseUrl } from './api/config'
import { AuthProvider } from './auth/AuthProvider'
import { useAuth } from './auth/auth'
import AppShell from './components/AppShell'
import StatusNotice from './components/StatusNotice'
import { InterviewProvider } from './features/interview/InterviewProvider'
import { useInterview } from './features/interview/useInterview'
import InterviewPage from './pages/InterviewPage'
import JobsPage from './pages/JobsPage'
import LoginPage from './pages/LoginPage'
import './App.css'
function useDesktop() {
  const [desktop, setDesktop] = useState(() => window.matchMedia('(min-width: 1024px)').matches)

  useEffect(() => {
    const media = window.matchMedia('(min-width: 1024px)')
    const update = () => setDesktop(media.matches)
    media.addEventListener('change', update)
    update()
    return () => media.removeEventListener('change', update)
  }, [])

  return desktop
}

function DesktopRequired() {
  const { user, logout } = useAuth()
  return (
    <div className="desktop-required" role="status">
      <p className="eyebrow">Desktop experience</p>
      <h1>Use a desktop to interview</h1>
      <p>Voice interviews require a browser window at least 1024 pixels wide. Widen this window or return on a laptop or desktop to continue your saved interview.</p>
      {user && <button className="button button--secondary" type="button" onClick={logout}>Log out</button>}
    </div>
  )
}

function NotFound() {
  const { user } = useAuth()
  return (
    <AppShell title="Page not found" eyebrow="Wrong turn">
      <StatusNotice tone="warning" action={<Link to={user ? '/jobs' : '/login'}>Go back</Link>}>
        This address does not match an available page. Your saved interview has not been changed.
      </StatusNotice>
    </AppShell>
  )
}

function ProtectedPages() {
  const { user, api } = useAuth()
  const location = useLocation()
  if (!user || !api) {
    const match = /^\/interviews\/([^/]+)$/.exec(location.pathname)
    const id = match && z.uuid().safeParse(match[1]).success ? match[1] : null
    const login = id ? `/login?interview=${encodeURIComponent(id)}` : '/login'
    return <Navigate to={login} replace />
  }
  return (
    <InterviewProvider key={user.id} api={api} ownerId={user.id}>
      <DesktopProtectedPages />
    </InterviewProvider>
  )
}

function DesktopProtectedPages() {
  const desktop = useDesktop()
  const { controller } = useInterview()
  useEffect(() => {
    const media = window.matchMedia('(min-width: 1024px)')
    const stopOnNarrow = () => {
      if (!media.matches) controller.leave()
    }
    media.addEventListener('change', stopOnNarrow)
    stopOnNarrow()
    return () => media.removeEventListener('change', stopOnNarrow)
  }, [controller])
  if (!desktop) return <DesktopRequired />
  return (
    <Routes>
      <Route path="jobs" element={<JobsPage />} />
      <Route path="interviews/:interviewId" element={<InterviewPage />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}

function Pages() {
  const { user } = useAuth()
  const location = useLocation()
  const interview = new URLSearchParams(location.search).get('interview')
  const authenticatedDestination = interview && z.uuid().safeParse(interview).success
    ? `/interviews/${encodeURIComponent(interview)}` : '/jobs'
  const desktop = useDesktop()
  return (
    <Routes>
      <Route path="/" element={<Navigate to={user ? '/jobs' : '/login'} replace />} />
      <Route path="/login" element={desktop ? user ? <Navigate to={authenticatedDestination} replace /> : <LoginPage /> : <DesktopRequired />} />
      <Route path="/*" element={<ProtectedPages />} />
    </Routes>
  )
}

export default function App() {
  let baseUrl: string
  try {
    baseUrl = readApiBaseUrl()
  } catch {
    return (
      <div className="config-required" role="alert">
        <h1>API configuration needed</h1>
        <p>Set <code>VITE_API_BASE_URL=http://localhost:8000/api/v1</code> when starting the frontend, then reload the page.</p>
      </div>
    )
  }
  return <AuthProvider baseUrl={baseUrl}><Pages /></AuthProvider>
}
