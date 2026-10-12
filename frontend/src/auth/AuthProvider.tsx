import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, createApiClient } from '../api/client'
import type { ApiClient } from '../api/client'
import type { User } from '../api/contracts'
import { AuthContext } from './auth'

type Session = { user: User; api: ApiClient }

export function AuthProvider({ children, baseUrl }: { children: ReactNode; baseUrl: string }) {
  const [session, setSession] = useState<Session | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)
  const loginPromise = useRef<Promise<void> | null>(null)
  const pendingLogin = useRef<AbortController | null>(null)
  const teardowns = useRef(new Set<() => void>())

  const registerTeardown = useCallback((callback: () => void) => {
    teardowns.current.add(callback)
    return () => { teardowns.current.delete(callback) }
  }, [])

  const releaseResources = useCallback(() => {
    for (const teardown of teardowns.current) {
      try {
        teardown()
      } catch {
        // One failing resource must not keep credentials or another resource alive.
      }
    }
  }, [])

  const logout = useCallback(() => {
    releaseResources()
    generation.current += 1
    pendingLogin.current?.abort()
    pendingLogin.current = null
    loginPromise.current = null
    setSession(null)
    setPending(false)
    setError(null)
  }, [releaseResources])

  const login = useCallback((email: string, password: string): Promise<void> => {
    if (loginPromise.current) return loginPromise.current
    const aborter = new AbortController()
    pendingLogin.current = aborter
    const attempt = ++generation.current
    setSession(null)
    setPending(true)
    setError(null)
    const operation = (async () => {
      try {
        const anonymous = createApiClient(baseUrl, null, () => {})
        const token = await anonymous.login(email, password, aborter.signal)
        if (generation.current !== attempt) throw new DOMException('Authentication was cancelled.', 'AbortError')
        const api = createApiClient(baseUrl, token.access_token, () => {
          if (generation.current !== attempt) return
          releaseResources()
          generation.current += 1
          pendingLogin.current?.abort()
          pendingLogin.current = null
          loginPromise.current = null
          setSession(null)
          setPending(false)
          setError('Session expired. Please sign in again.')
        })
        const user = await api.getMe(aborter.signal)
        if (generation.current !== attempt) throw new DOMException('Authentication was cancelled.', 'AbortError')
        setSession({ user, api })
      } catch (cause) {
        if (generation.current !== attempt) throw cause
        setError(cause instanceof ApiError ? cause.message : 'Unable to sign in. Please try again.')
        throw cause
      } finally {
        if (generation.current === attempt) {
          pendingLogin.current = null
          loginPromise.current = null
          setPending(false)
        }
      }
    })()
    loginPromise.current = operation
    return operation
  }, [baseUrl, releaseResources])

  useEffect(() => () => {
    generation.current += 1
    pendingLogin.current?.abort()
    pendingLogin.current = null
    loginPromise.current = null
  }, [])

  const value = useMemo(() => ({
    user: session?.user ?? null,
    api: session?.api ?? null,
    login, logout, pending, error, registerTeardown,
  }), [session, login, logout, pending, error, registerTeardown])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
