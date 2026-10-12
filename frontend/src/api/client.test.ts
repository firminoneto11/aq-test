import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, createApiClient } from './client'
import type { InterviewState } from './contracts'

const jobId = '11111111-1111-4111-8111-111111111111'
const interviewId = '22222222-2222-4222-8222-222222222222'
const requestId = '33333333-3333-4333-8333-333333333333'
const runId = '44444444-4444-4444-8444-444444444444'
const state: InterviewState = {
  id: interviewId,
  job: { id: jobId, slug: 'backend-engineer', title: 'Backend Engineer', description: '', pack_version: 1 },
  status: 'created', revision: 0, question_count: 0, answered_count: 0, follow_up_count: 0,
  current_question: null, answerable_question_id: null, rubric: [], topics_covered: [], gaps: [],
  skills_detected: [], decision: null, evaluation_status: 'not_ready', last_error_code: null, can_resume: true,
}

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
})
const api = () => createApiClient('http://localhost:8000/api/v1', 'application-jwt', () => {})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('API status and wire validation', () => {
  it('uses form credentials only for login and bearer/no-store only on private requests', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ access_token: 'jwt', token_type: 'bearer', expires_in: 86400 }))
      .mockResolvedValueOnce(json({ id: jobId, email: 'reviewer@example.com' }))
      .mockResolvedValueOnce(json({ jobs: [] }))
    vi.stubGlobal('fetch', fetcher)
    const client = api()
    await client.login('reviewer@example.com', 'secret')
    await client.getMe()
    await client.getJobs()
    expect(fetcher.mock.calls[0][0]).toBe('http://localhost:8000/api/v1/auth/token')
    const login = fetcher.mock.calls[0][1] as RequestInit
    expect(login.body).toBeInstanceOf(URLSearchParams)
    expect(String(login.body)).toBe('username=reviewer%40example.com&password=secret')
    expect(new Headers(login.headers).get('Authorization')).toBeNull()
    expect(new Headers(login.headers).get('Content-Type')).toBe('application/x-www-form-urlencoded')
    const me = fetcher.mock.calls[1][1] as RequestInit
    expect(new Headers(me.headers).get('Authorization')).toBe('Bearer application-jwt')
    expect(me.cache).toBe('no-store')
    const jobs = fetcher.mock.calls[2][1] as RequestInit
    expect(new Headers(jobs.headers).get('Authorization')).toBeNull()
  })

  it('accepts only specified 200/201 create and discriminates pending from grant by HTTP status', async () => {
    const pending = { interview_id: interviewId, run_id: runId, status: 'provisioning', retry_after_ms: 250 }
    const grant = {
      interview_id: interviewId, run_id: runId, state, server_url: 'wss://livekit.example.test',
      room_name: 'room', participant_identity: 'candidate', participant_token: 'room-token',
      expires_at: '2026-10-12T10:00:00+00:00',
    }
    const fetcher = vi.fn().mockResolvedValueOnce(json(state, 201)).mockResolvedValueOnce(json(state))
      .mockResolvedValueOnce(json(pending, 202)).mockResolvedValueOnce(json(grant))
      .mockResolvedValueOnce(json(pending, 200)).mockResolvedValueOnce(json(grant, 202))
    vi.stubGlobal('fetch', fetcher)
    expect((await api().createInterview(jobId, requestId)).status).toBe(201)
    expect((await api().createInterview(jobId, requestId)).status).toBe(200)
    expect((await api().connectInterview(interviewId, requestId)).status).toBe(202)
    expect((await api().connectInterview(interviewId, requestId)).status).toBe(200)
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({ kind: 'protocol', status: 200 })
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({ kind: 'protocol', status: 202 })
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ request_id: requestId })
    expect(new Headers(fetcher.mock.calls[2][1].headers).get('Content-Type')).toBe('application/json')
  })

  it('rejects zero and overflow retry intervals instead of scheduling a rapid provisioning loop', async () => {
    const pending = { interview_id: interviewId, run_id: runId, status: 'provisioning' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ ...pending, retry_after_ms: 0 }, 202))
      .mockResolvedValueOnce(json({ ...pending, retry_after_ms: 2_147_483_648 }, 202)))
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({ kind: 'protocol', status: 202 })
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({ kind: 'protocol', status: 202 })
  })

  it('rejects malformed successful JSON, proxy HTML, incomplete and unexpected-status success envelopes', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{invalid', { status: 200 }))
      .mockResolvedValueOnce(new Response('<html>proxy</html>', { status: 200 }))
      .mockResolvedValueOnce(json({ jobs: [{ id: jobId, title: 'missing fields' }] }))
      .mockResolvedValueOnce(json(state, 202))
    vi.stubGlobal('fetch', fetcher)
    await expect(api().getJobs()).rejects.toMatchObject({ kind: 'protocol', status: 200 })
    await expect(api().getJobs()).rejects.toMatchObject({ kind: 'protocol', status: 200 })
    await expect(api().getJobs()).rejects.toMatchObject({ kind: 'protocol', status: 200 })
    await expect(api().createInterview(jobId, requestId)).rejects.toMatchObject({ kind: 'protocol', status: 202 })
  })

  it('rejects invalid timestamps, UUIDs, fractional or out-of-range scores, and strips unneeded payload properties', async () => {
    const grant = {
      interview_id: interviewId, run_id: runId, state, server_url: 'wss://livekit.example.test',
      room_name: 'room', participant_identity: 'candidate', participant_token: 'room-token', expires_at: '2026-10-12T10:00:00',
    }
    const fetcher = vi.fn().mockResolvedValueOnce(json(grant))
      .mockResolvedValueOnce(json({ ...state, id: 'invalid' }))
      .mockResolvedValueOnce(json({ status: 'completed', result: {
        strengths: [], concerns: [], rubric_scores: [{ topic_id: 'api', score: 5, answer_ids: [] }],
        overall_score: 101, insufficient_evidence: false, gaps: [],
      }, error_code: null, can_retry: false }))
      .mockResolvedValueOnce(json({ status: 'completed', result: {
        strengths: [], concerns: [], rubric_scores: ['ownership', 'api_design', 'data_consistency', 'reliability'].map((topic_id, index) => ({
          topic_id, score: index === 0 ? 2.5 : 0, answer_ids: [],
        })),
        overall_score: 12.5, insufficient_evidence: false, gaps: [],
      }, error_code: null, can_retry: false }))
      .mockResolvedValueOnce(json({ jobs: [], database_secret: 'do-not-expose' }))
    vi.stubGlobal('fetch', fetcher)
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({ kind: 'protocol' })
    await expect(api().getInterview(interviewId)).rejects.toMatchObject({ kind: 'protocol' })
    await expect(api().getEvaluation(interviewId)).rejects.toMatchObject({ kind: 'protocol' })
    await expect(api().getEvaluation(interviewId)).rejects.toMatchObject({ kind: 'protocol' })
    expect(await api().getJobs()).toEqual({ jobs: [] })
  })
  it('preserves evaluation retry status and genuine zero scores without sending a body', async () => {
    const running = { status: 'running', result: null, error_code: null, can_retry: true }
    const completed = {
      status: 'completed', result: {
        strengths: [], concerns: [], rubric_scores: ['api', 'sql', 'design', 'reliability'].map((topic_id) => ({
          topic_id, score: 0, answer_ids: [],
        })),
        overall_score: 0, insufficient_evidence: true, gaps: ['SQL'],
      }, error_code: null, can_retry: false,
    }
    const fetcher = vi.fn().mockResolvedValueOnce(json(running, 202)).mockResolvedValueOnce(json(completed))
    vi.stubGlobal('fetch', fetcher)
    expect(await api().retryEvaluation(interviewId)).toEqual({ status: 202, data: running })
    expect(await api().getEvaluation(interviewId)).toEqual(completed)
    expect(fetcher.mock.calls[0][1].body).toBeUndefined()
    expect(new Headers(fetcher.mock.calls[0][1].headers).get('Content-Type')).toBeNull()
  })

  it('preserves completed/null envelopes for UI integration errors, but rejects missing or duplicate completed rubric topics', async () => {
    const withoutResult = { status: 'completed', result: null, error_code: null, can_retry: false }
    const incomplete = {
      ...withoutResult,
      result: {
        strengths: [], concerns: [], rubric_scores: [{ topic_id: 'api', score: 0, answer_ids: [] }],
        overall_score: 0, insufficient_evidence: false, gaps: [],
      },
    }
    const duplicate = {
      ...incomplete,
      result: {
        ...incomplete.result,
        rubric_scores: ['api', 'sql', 'design', 'api'].map((topic_id) => ({ topic_id, score: 0, answer_ids: [] })),
      },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(withoutResult))
      .mockResolvedValueOnce(json(incomplete)).mockResolvedValueOnce(json(duplicate)))
    expect(await api().getEvaluation(interviewId)).toEqual(withoutResult)
    await expect(api().getEvaluation(interviewId)).rejects.toMatchObject({ kind: 'protocol' })
    await expect(api().getEvaluation(interviewId)).rejects.toMatchObject({ kind: 'protocol' })
  })

})

describe('API failure and cancellation semantics', () => {
  it('preserves safe string and FastAPI validation detail, without exposing proxy HTML', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ detail: 'Voice capacity reached' }, 429))
      .mockResolvedValueOnce(json({ detail: [{ loc: ['body', 'request_id'], msg: 'Invalid UUID', type: 'value_error' }] }, 422))
      .mockResolvedValueOnce(new Response('<html>private proxy trace</html>', { status: 503 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({
      kind: 'http', status: 429, message: 'Voice capacity reached', outcomeUnknown: false,
    })
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({
      kind: 'http', status: 422, fieldMessages: ['body.request_id: Invalid UUID'],
    })
    await expect(api().connectInterview(interviewId, requestId)).rejects.toMatchObject({
      kind: 'http', status: 503, message: 'Request failed (HTTP 503).',
    })
  })

  it('does not invalidate authentication on login 401 and never exposes user enumeration', async () => {
    const unauthorized = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'Unknown user reviewer@example.com' }, 401)))
    await expect(createApiClient('http://localhost:8000/api/v1', null, unauthorized).login('reviewer@example.com', 'bad'))
      .rejects.toMatchObject({ kind: 'http', status: 401, message: 'Invalid email or password.' })
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it('invalidates on protected 401, but an old session callback cannot clear a newer login', async () => {
    let generation = 1
    let active = 'old'
    const old = createApiClient('http://localhost:8000/api/v1', 'old-token', () => {
      if (generation === 1) active = 'logged-out'
    })
    generation = 2
    active = 'new'
    const current = createApiClient('http://localhost:8000/api/v1', 'new-token', () => {
      if (generation === 2) active = 'logged-out'
    })
    const fetcher = vi.fn().mockResolvedValue(json({ detail: 'expired' }, 401))
    vi.stubGlobal('fetch', fetcher)
    await expect(old.getInterview(interviewId)).rejects.toMatchObject({ kind: 'http', status: 401 })
    expect(active).toBe('new')
    await expect(current.getInterview(interviewId)).rejects.toMatchObject({ kind: 'http', status: 401 })
    expect(active).toBe('logged-out')
    expect(new Headers(fetcher.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer old-token')
    expect(new Headers(fetcher.mock.calls[1][1].headers).get('Authorization')).toBe('Bearer new-token')
  })

  it('keeps a protected HTTP 401 distinguishable when invalidation cancels its caller signal', async () => {
    const aborter = new AbortController()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'expired' }, 401)))
    const client = createApiClient('http://localhost:8000/api/v1', 'expired-token', () => aborter.abort())
    await expect(client.getMe(aborter.signal)).rejects.toMatchObject({
      kind: 'http', status: 401, message: 'Session expired. Please sign in again.',
    })
    expect(aborter.signal.aborted).toBe(true)
  })

  it('marks network/timeout POST outcome unknown, never retries automatically, and differentiates caller abort', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('offline'))
      .mockImplementationOnce((_url: string, options: RequestInit) => {
        const { promise, reject } = Promise.withResolvers<Response>()
        options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        return promise
      })
      .mockImplementationOnce((_url: string, options: RequestInit) => {
        const { promise, reject } = Promise.withResolvers<Response>()
        options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        return promise
      })
    vi.stubGlobal('fetch', fetcher)
    await expect(api().createInterview(jobId, requestId)).rejects.toMatchObject({
      kind: 'network', outcomeUnknown: true, status: null,
    })
    const timedOut = api().createInterview(jobId, requestId)
    const timeoutAssertion = expect(timedOut).rejects.toMatchObject({ kind: 'timeout', outcomeUnknown: true })
    await vi.advanceTimersByTimeAsync(30_000)
    await timeoutAssertion
    const abort = new AbortController()
    const cancelled = api().createInterview(jobId, requestId, abort.signal)
    const cancelAssertion = expect(cancelled).rejects.toMatchObject({ name: 'AbortError' })
    abort.abort()
    await cancelAssertion
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('rejects invalid request IDs without sending a request', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    await expect(api().endInterview('../../unowned')).rejects.toBeInstanceOf(ApiError)
    expect(fetcher).not.toHaveBeenCalled()
  })
})
