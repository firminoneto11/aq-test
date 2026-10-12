import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { z } from 'zod'
import { useAuth } from '../auth/auth'
import AppShell from '../components/AppShell'
import StatusNotice from '../components/StatusNotice'

export default function LoginPage() {
  const { login, pending, error: sessionError } = useAuth()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const submitting = useRef(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const interview = searchParams.get('interview')
  const returnTo = interview && z.uuid().safeParse(interview).success
    ? `/interviews/${encodeURIComponent(interview)}` : '/jobs'

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting.current) return
    submitting.current = true
    setError(null)
    try {
      await login(email.trim(), password)
      navigate(returnTo, { replace: true })
    } catch {
      setError('Login failed. Check your credentials and connection, then try again.')
    } finally {
      submitting.current = false
      setPassword('')
    }
  }

  return (
    <AppShell title="Welcome back" eyebrow="AI Interviewer">
      <div className="login">
        <div className="card stack--large">
          <div>
            <h2>Sign in to continue</h2>
            <p className="lead">Your interviewer is ready when you are. Enter the reviewer account provided by the operator.</p>
          </div>
          {(error ?? sessionError) && <StatusNotice tone="error">{error ?? sessionError}</StatusNotice>}
          <form className="login__form" onSubmit={(event) => { void submit(event) }}>
            <div className="field">
              <label htmlFor="email">Email address</label>
              <input id="email" type="email" name="username" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input id="password" type="password" name="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
            </div>
            <button className="button" type="submit" disabled={pending}>{pending ? 'Signing in…' : 'Sign in'}</button>
          </form>
          <p className="muted">No account yet? Ask the operator to provision a reviewer login. There is no public signup or password recovery in this demo.</p>
        </div>
      </div>
    </AppShell>
  )
}
