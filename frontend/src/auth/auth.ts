import { createContext, useContext } from 'react'
import type { ApiClient } from '../api/client'
import type { User } from '../api/contracts'

export interface AuthState {
  user: User | null
  api: ApiClient | null
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  registerTeardown: (callback: () => void) => () => void
  pending: boolean
  error: string | null
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const auth = useContext(AuthContext)
  if (!auth) throw new Error('useAuth must be used inside AuthProvider')
  return auth
}
