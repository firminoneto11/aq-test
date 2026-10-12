import { z } from 'zod'
import {
  connectionGrantSchema, connectionPendingSchema, evaluationResponseSchema,
  interviewStateSchema, jobsResponseSchema, loginResponseSchema, transcriptResponseSchema,
  userSchema, uuidSchema,
} from './contracts'
import type {
  ConnectionGrant, ConnectionPending, EvaluationResponse, InterviewState,
  JobsResponse, LoginResponse, TranscriptResponse, User,
} from './contracts'

export type ApiErrorKind = 'http' | 'network' | 'timeout' | 'protocol'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status: number | null
  readonly fieldMessages?: string[]
  readonly outcomeUnknown: boolean

  constructor(kind: ApiErrorKind, message: string, status: number | null = null, fieldMessages?: string[], outcomeUnknown = false) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.fieldMessages = fieldMessages
    this.outcomeUnknown = outcomeUnknown
  }
}

export interface ApiClient {
  login(email: string, password: string, signal?: AbortSignal): Promise<LoginResponse>
  getMe(signal?: AbortSignal): Promise<User>
  getJobs(signal?: AbortSignal): Promise<JobsResponse>
  createInterview(jobId: string, requestId: string, signal?: AbortSignal): Promise<{ status: 200 | 201; data: InterviewState }>
  getInterview(id: string, signal?: AbortSignal): Promise<InterviewState>
  connectInterview(id: string, requestId: string, signal?: AbortSignal): Promise<{ status: 200; data: ConnectionGrant } | { status: 202; data: ConnectionPending }>
  getTranscript(id: string, signal?: AbortSignal): Promise<TranscriptResponse>
  getEvaluation(id: string, signal?: AbortSignal): Promise<EvaluationResponse>
  retryEvaluation(id: string, signal?: AbortSignal): Promise<{ status: 200 | 202; data: EvaluationResponse }>
  endInterview(id: string, signal?: AbortSignal): Promise<InterviewState>
}

const HTTP_DEADLINE_MS = 30_000
const controlCharacters = /\p{Cc}/gu

function validId(id: string): string {
  if (!uuidSchema.safeParse(id).success) throw new ApiError('protocol', 'Invalid request identifier.')
  return encodeURIComponent(id)
}

function validationMessages(detail: unknown): string[] {
  if (!Array.isArray(detail)) return []
  return detail.slice(0, 8).flatMap((entry: unknown) => {
    if (typeof entry !== 'object' || entry === null || !('msg' in entry) || typeof entry.msg !== 'string') return []
    const location = 'loc' in entry && Array.isArray(entry.loc)
      ? entry.loc.filter((part: unknown) => (typeof part === 'string' || typeof part === 'number') && String(part).length <= 40).slice(0, 5).join('.')
      : ''
    const message = entry.msg.replace(controlCharacters, ' ').trim()
    if (!message || message.length > 200 || /[<>]/.test(message)) return []
    return [`${location ? `${location}: ` : ''}${message}`]
  })
}

async function httpError(response: Response, protectedRequest: boolean, loginRequest: boolean): Promise<ApiError> {
  if (response.status === 401) {
    return new ApiError('http', loginRequest ? 'Invalid email or password.' : protectedRequest ? 'Session expired. Please sign in again.' : 'Request failed (HTTP 401).', 401)
  }
  const fallback = `Request failed (HTTP ${response.status}).`
  let parsed: unknown
  try {
    parsed = await response.json()
  } catch {
    return new ApiError('http', fallback, response.status)
  }
  if (typeof parsed !== 'object' || parsed === null || !('detail' in parsed)) {
    return new ApiError('http', fallback, response.status)
  }
  const { detail } = parsed
  if (typeof detail === 'string') {
    const message = detail.replace(controlCharacters, ' ').trim()
    if (message && message.length <= 240 && !/[<>]/.test(message)) return new ApiError('http', message, response.status)
  }
  const fields = validationMessages(detail)
  if (fields.length) return new ApiError('http', 'Check the request fields and try again.', response.status, fields)
  return new ApiError('http', protectedRequest && response.status === 403 ? 'Access denied.' : fallback, response.status)
}

export function createApiClient(baseUrl: string, accessToken: string | null, onUnauthorized: () => void): ApiClient {
  const root = baseUrl.replace(/\/+$/, '')

  async function request<T>(
    path: string,
    method: 'GET' | 'POST',
    schema: z.ZodType<T>,
    statuses: readonly number[],
    options: { protected?: boolean; body?: string | URLSearchParams; contentType?: string; signal?: AbortSignal; login?: boolean } = {},
  ): Promise<{ status: number; data: T }> {
    const authenticated = options.protected === true
    if (authenticated && !accessToken) throw new ApiError('protocol', 'Sign in to continue.')
    const { signal } = options
    if (signal?.aborted) throw new DOMException('Request cancelled.', 'AbortError')
    const controller = new AbortController()
    const abort = { reason: null as 'caller' | 'timeout' | null }
    const onAbort = () => {
      if (abort.reason === null) {
        abort.reason = 'caller'
        controller.abort()
      }
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    const timer = setTimeout(() => {
      if (abort.reason === null) {
        abort.reason = 'timeout'
        controller.abort()
      }
    }, HTTP_DEADLINE_MS)
    const headers = new Headers()
    if (authenticated) headers.set('Authorization', `Bearer ${accessToken}`)
    if (options.contentType) headers.set('Content-Type', options.contentType)
    const mutating = method === 'POST' && !options.login
    try {
      const response = await fetch(`${root}${path}`, {
        method,
        headers,
        body: options.body,
        signal: controller.signal,
        credentials: 'omit',
        redirect: 'error',
        ...(method === 'GET' && authenticated ? { cache: 'no-store' as const } : {}),
      })
      if (abort.reason === 'caller') throw new DOMException('Request cancelled.', 'AbortError')
      if (abort.reason === 'timeout') throw new ApiError('timeout', 'Request timed out. The server may still have processed it.', null, undefined, mutating)
      if (response.status === 401 && authenticated) onUnauthorized()
      if (!statuses.includes(response.status)) {
        if (response.status < 200 || response.status >= 300) throw await httpError(response, authenticated, options.login === true)
        throw new ApiError('protocol', `Unexpected HTTP success status (${response.status}).`, response.status)
      }
      let payload: unknown
      try {
        payload = await response.json()
      } catch {
        if (abort.reason === 'caller') throw new DOMException('Request cancelled.', 'AbortError')
        if (abort.reason === 'timeout') throw new ApiError('timeout', 'Request timed out. The server may still have processed it.', null, undefined, mutating)
        throw new ApiError('protocol', 'The server returned an invalid JSON response.', response.status)
      }
      if (abort.reason === 'caller') throw new DOMException('Request cancelled.', 'AbortError')
      if (abort.reason === 'timeout') throw new ApiError('timeout', 'Request timed out. The server may still have processed it.', null, undefined, mutating)
      const parsed = schema.safeParse(payload)
      if (!parsed.success) {
        const fields = parsed.error.issues.slice(0, 8).map((issue) => issue.path.join('.') || 'response')
        throw new ApiError('protocol', 'The server returned an unexpected response.', response.status, fields)
      }
      return { status: response.status, data: parsed.data }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) throw error
      if (abort.reason === 'caller') throw new DOMException('Request cancelled.', 'AbortError')
      if (abort.reason === 'timeout') throw new ApiError('timeout', 'Request timed out. The server may still have processed it.', null, undefined, mutating)
      if (error instanceof ApiError) throw error
      throw new ApiError('network', 'Cannot reach the service. Check your connection and try again.', null, undefined, mutating)
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
  }

  return {
    async login(email, password, signal) {
      const result = await request('/auth/token', 'POST', loginResponseSchema, [200], {
        body: new URLSearchParams({ username: email, password }),
        contentType: 'application/x-www-form-urlencoded', signal, login: true,
      })
      return result.data
    },
    async getMe(signal) {
      return (await request('/auth/me', 'GET', userSchema, [200], { protected: true, signal })).data
    },
    async getJobs(signal) {
      return (await request('/jobs', 'GET', jobsResponseSchema, [200], { signal })).data
    },
    async createInterview(jobId, requestId, signal) {
      validId(jobId)
      validId(requestId)
      const result = await request('/interviews', 'POST', interviewStateSchema, [200, 201], {
        protected: true, body: JSON.stringify({ job_id: jobId, request_id: requestId }), contentType: 'application/json', signal,
      })
      if (result.data.job.id !== jobId) throw new ApiError('protocol', 'The server returned a different job.', result.status)
      return result as { status: 200 | 201; data: InterviewState }
    },
    async getInterview(id, signal) {
      const result = await request(`/interviews/${validId(id)}`, 'GET', interviewStateSchema, [200], { protected: true, signal })
      if (result.data.id !== id) throw new ApiError('protocol', 'The server returned a different interview.', result.status)
      return result.data
    },
    async connectInterview(id, requestId, signal) {
      const route = `/interviews/${validId(id)}/connect`
      validId(requestId)
      const result = await request(route, 'POST', z.union([connectionGrantSchema, connectionPendingSchema]), [200, 202], {
        protected: true, body: JSON.stringify({ request_id: requestId }), contentType: 'application/json', signal,
      })
      if (result.data.interview_id !== id || ('state' in result.data && result.data.state.id !== id)) {
        throw new ApiError('protocol', 'The server returned a different interview.', result.status)
      }
      if (result.status === 200 && 'state' in result.data) return { status: 200, data: result.data }
      if (result.status === 202 && 'status' in result.data && result.data.status === 'provisioning') {
        return { status: 202, data: result.data }
      }
      throw new ApiError('protocol', 'The server returned an unexpected connection status.', result.status)
    },
    async getTranscript(id, signal) {
      const result = await request(`/interviews/${validId(id)}/transcript`, 'GET', transcriptResponseSchema, [200], { protected: true, signal })
      if (result.data.interview_id !== id) throw new ApiError('protocol', 'The server returned a different interview.', result.status)
      return result.data
    },
    async getEvaluation(id, signal) {
      return (await request(`/interviews/${validId(id)}/evaluation`, 'GET', evaluationResponseSchema, [200], { protected: true, signal })).data
    },
    async retryEvaluation(id, signal) {
      const result = await request(`/interviews/${validId(id)}/evaluation`, 'POST', evaluationResponseSchema, [200, 202], { protected: true, signal })
      return result as { status: 200 | 202; data: EvaluationResponse }
    },
    async endInterview(id, signal) {
      const result = await request(`/interviews/${validId(id)}/end`, 'POST', interviewStateSchema, [200], { protected: true, signal })
      if (result.data.id !== id) throw new ApiError('protocol', 'The server returned a different interview.', result.status)
      return result.data
    },
  }
}
