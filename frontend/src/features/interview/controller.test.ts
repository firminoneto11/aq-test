import { EventEmitter } from 'node:events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConnectionState, RoomEvent } from 'livekit-client'
import type { LocalAudioTrack, Room } from 'livekit-client'
import type { ApiClient } from '../../api/client'
import { ApiError } from '../../api/client'
import type { InterviewState, ConnectionGrant, TranscriptResponse, EvaluationResponse } from '../../api/contracts'
import { InterviewController, resolveQuestions } from './controller'
import type { MediaBoundary } from './controller'

const OWNER = '4b7896a7-c9c0-4ad1-9725-10841b6855fb'
const JOB = 'c008148f-ad81-4bd0-a705-31109551ac11'
const ID = '940de8d5-e0c4-42cf-9ca6-94ee9aba9fe8'
const QUESTION = '8bc3cbb6-d89a-4f13-a4fa-a05023e403ac'
const NEXT = '4797682d-1a90-4d16-b7b0-63e3c59c1e96'
const RUN = '02e984fc-6a3e-48a1-93af-bd886a18ed57'

function deferred<T>() { return Promise.withResolvers<T>() }
function state(overrides: Partial<InterviewState> = {}): InterviewState {
  return {
    id: ID, job: { id: JOB, slug: 'backend', title: 'Backend Engineer', description: 'Systems', pack_version: 1 },
    status: 'created', revision: 0, question_count: 0, answered_count: 0, follow_up_count: 0,
    current_question: null, answerable_question_id: null, rubric: [], topics_covered: [], gaps: [],
    skills_detected: [], decision: null, evaluation_status: 'not_ready', last_error_code: null,
    can_resume: true, ...overrides,
  }
}
const transcript: TranscriptResponse = { interview_id: ID, status: 'created', questions: [], messages: [] }
const evaluation: EvaluationResponse = { status: 'not_ready', result: null, error_code: null, can_retry: false }
const completedEvaluation: EvaluationResponse = {
  status: 'completed', result: {
    strengths: [], concerns: [],
    rubric_scores: ['ownership', 'api_design', 'data_consistency', 'reliability'].map(topic_id => ({
      topic_id, score: 0, answer_ids: [],
    })),
    overall_score: 0, insufficient_evidence: true,
    gaps: ['ownership', 'api_design', 'data_consistency', 'reliability'],
  },
  error_code: null, can_retry: false,
}
const grant: ConnectionGrant = {
  interview_id: ID, run_id: RUN, server_url: 'wss://voice.example', room_name: 'room',
  participant_identity: 'candidate', participant_token: 'secret', expires_at: '2099-01-01T00:00:00+00:00', state: state({ status: 'active' }),
}

class FakeRoom extends EventEmitter {
  state = ConnectionState.Connected
  canPlaybackAudio = true
  connect = vi.fn(async () => {})
  disconnect = vi.fn(async () => {})
  startAudio = vi.fn(async () => {})
  registerTextStreamHandler = vi.fn()
  unregisterTextStreamHandler = vi.fn()
  localParticipant = { publishTrack: vi.fn(async () => {}), unpublishTrack: vi.fn(async () => {}) }
}

function setup() {
  const storage = new Map<string, string>()
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value) },
    removeItem: (key: string) => { storage.delete(key) },
  })
  vi.stubGlobal('window', {
    isSecureContext: true, innerWidth: 1440,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn() } })
  vi.stubGlobal('document', { visibilityState: 'visible', addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const rooms: FakeRoom[] = []
  const tracks: LocalAudioTrack[] = []
  const media: MediaBoundary = {
    createTrack: vi.fn(async () => {
      const track = {
        isMuted: false, stop: vi.fn(), mute: vi.fn(async () => {}), unmute: vi.fn(async () => {}),
        mediaStreamTrack: { addEventListener: vi.fn() },
      } as unknown as LocalAudioTrack
      tracks.push(track)
      return track
    }),
    createRoom: () => {
      const room = new FakeRoom()
      rooms.push(room)
      return room as unknown as Room
    },
  }
  const api = {
    createInterview: vi.fn<ApiClient['createInterview']>(async () => ({ status: 201, data: state() })),
    getInterview: vi.fn<ApiClient['getInterview']>(async () => state()),
    connectInterview: vi.fn<ApiClient['connectInterview']>(async () => ({ status: 200, data: grant })),
    getTranscript: vi.fn<ApiClient['getTranscript']>(async () => transcript),
    getEvaluation: vi.fn<ApiClient['getEvaluation']>(async () => evaluation),
    endInterview: vi.fn<ApiClient['endInterview']>(async () => state({ status: 'cancelled' })),
    retryEvaluation: vi.fn<ApiClient['retryEvaluation']>(async () => ({ status: 202, data: evaluation })),
  }
  const controller = new InterviewController(api as unknown as ApiClient, OWNER, media)
  controller.activate()
  return { controller, api, media, rooms, tracks, storage }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('interview admission', () => {
  it('double Start preserves one create UUID; pending 202 never joins until same-UUID 200', async () => {
    const { controller, api, rooms } = setup()
    const created = deferred<{ status: 201; data: InterviewState }>()
    api.createInterview.mockImplementationOnce(() => created.promise)
    const admitted = deferred<{ status: 200; data: ConnectionGrant }>()
    api.connectInterview.mockImplementationOnce(async () => ({ status: 202, data: { interview_id: ID, run_id: RUN, status: 'provisioning', retry_after_ms: 500 } }))
      .mockImplementationOnce(() => admitted.promise)
    const onCreated = vi.fn()
    const start = controller.start(JOB, onCreated)
    const duplicate = controller.start(JOB, onCreated)
    await duplicate
    await vi.waitFor(() => expect(api.createInterview).toHaveBeenCalledTimes(1))
    created.resolve({ status: 201, data: state() })
    await vi.waitFor(() => expect(api.connectInterview).toHaveBeenCalledTimes(1))
    expect(rooms).toHaveLength(0)
    expect(controller.getSnapshot().phase).toBe('provisioning')
    await vi.advanceTimersByTimeAsync(500)
    expect(api.connectInterview).toHaveBeenCalledTimes(2)
    expect(api.connectInterview.mock.calls[0][1]).toBe(api.connectInterview.mock.calls[1][1])
    admitted.resolve({ status: 200, data: grant })
    await start
    expect(rooms[0].localParticipant.publishTrack).toHaveBeenCalledTimes(1)
    expect(onCreated).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().pendingConnect).toBeNull()
    controller.deactivate()
  })

  it('can explicitly start another role after leaving a previously opened interview', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    controller.leave()
    await controller.start(JOB, vi.fn())
    expect(api.createInterview).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().phase).toBe('connected')
    controller.deactivate()
  })

  it('stale microphone permission never reserves a voice slot', async () => {
    const { controller, api, media } = setup()
    const permission = deferred<LocalAudioTrack>()
    const stop = vi.fn()
    vi.mocked(media.createTrack).mockImplementationOnce(() => permission.promise)
    const start = controller.start(JOB, vi.fn())
    controller.leave()
    permission.resolve({ stop } as unknown as LocalAudioTrack)
    await start
    expect(stop).toHaveBeenCalledOnce()
    expect(api.createInterview).not.toHaveBeenCalled()
    expect(api.connectInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })

  it('resizing below desktop width during permission request stops late capture before creating', async () => {
    const { controller, api, media } = setup()
    const permission = deferred<LocalAudioTrack>()
    const stop = vi.fn()
    vi.mocked(media.createTrack).mockImplementationOnce(() => permission.promise)
    const start = controller.start(JOB, vi.fn())
    const resize = vi.mocked(window.addEventListener).mock.calls.find(([event]) => event === 'resize')?.[1]
    Object.defineProperty(window, 'innerWidth', { value: 800, configurable: true })
    if (typeof resize === 'function') resize(new Event('resize'))
    permission.resolve({ stop } as unknown as LocalAudioTrack)
    await start
    expect(stop).toHaveBeenCalledOnce()
    expect(api.createInterview).not.toHaveBeenCalled()
    expect(api.connectInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })

  it('uncertain creation retries its original UUID and a capacity failure keeps the connection UUID', async () => {
    const { controller, api } = setup()
    api.createInterview.mockRejectedValueOnce(new ApiError('timeout', 'Outcome unknown', null, undefined, true))
    await controller.start(JOB, vi.fn())
    expect(controller.getSnapshot().pendingCreate?.requestId).toBeTruthy()
    const original = api.createInterview.mock.calls[0][1]
    api.connectInterview.mockRejectedValueOnce(new ApiError('http', 'Voice capacity reached', 429))
    await controller.start(JOB, vi.fn())
    expect(api.createInterview.mock.calls[1][1]).toBe(original)
    const connection = controller.getSnapshot().pendingConnect?.requestId
    expect(connection).toBeTruthy()
    await controller.retryConnection()
    expect(api.connectInterview.mock.calls.at(-1)?.[1]).toBe(connection)
    controller.deactivate()
  })

  it('expired attempts clear pending UUID but terminal 409 does not rotate or reconnect automatically', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    api.connectInterview.mockRejectedValueOnce(new ApiError('http', 'Connection attempt expired', 409))
    await controller.resume()
    expect(controller.getSnapshot().pendingConnect).toBeNull()
    expect(controller.getSnapshot().error).toMatch(/Resume deliberately/)
    api.connectInterview.mockRejectedValueOnce(new ApiError('http', 'Interview cannot be resumed', 409))
    await controller.resume()
    expect(api.connectInterview).toHaveBeenCalledTimes(2)
    expect(controller.getSnapshot().error).toMatch(/cannot be resumed/)
    controller.deactivate()
  })

  it('leave during room connect tears down stale room without publication or End', async () => {
    const { controller, api, media, rooms, tracks } = setup()
    const connecting = deferred<void>()
    const createRoom = media.createRoom
    media.createRoom = () => {
      const room = createRoom() as unknown as FakeRoom
      room.connect.mockImplementationOnce(() => connecting.promise)
      return room as unknown as Room
    }
    await controller.open(ID)
    const admission = controller.resume()
    await vi.waitFor(() => expect(rooms).toHaveLength(1))
    expect(rooms[0].connect).toHaveBeenCalledOnce()
    // Replacing the room while the SDK is connecting fences its asynchronous continuation.
    controller.leave()
    connecting.resolve()
    await admission
    expect(rooms[0].localParticipant.publishTrack).not.toHaveBeenCalled()
    expect(rooms[0].disconnect).toHaveBeenCalled()
    expect(tracks[0].stop).toHaveBeenCalled()
    expect(api.endInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })
})

describe('authenticated room invalidations', () => {
  it('ignores malformed packets but re-fetches an equal-revision hint while a GET is pending', async () => {
    const { controller, api, rooms } = setup()
    await controller.open(ID)
    await controller.resume()
    const waiting = deferred<InterviewState>()
    api.getInterview.mockImplementationOnce(() => waiting.promise)
    const pending = controller.refresh()
    rooms[0].emit(RoomEvent.DataReceived, new TextEncoder().encode('{bad'), undefined, undefined, 'interview.state')
    rooms[0].emit(RoomEvent.DataReceived, new TextEncoder().encode(JSON.stringify({
      type: 'interview.state_changed', interview_id: ID, revision: 0,
    })), undefined, undefined, 'interview.state')
    waiting.resolve(state({ current_question: null }))
    await pending
    await vi.waitFor(() => expect(api.getInterview).toHaveBeenCalledTimes(4))
    expect(api.endInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })

  it('activation replay and Leave never create a run or send End', () => {
    const { controller, api, media } = setup()
    controller.deactivate()
    controller.activate()
    controller.leave()
    expect(media.createTrack).not.toHaveBeenCalled()
    expect(api.createInterview).not.toHaveBeenCalled()
    expect(api.connectInterview).not.toHaveBeenCalled()
    expect(api.endInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })
})

describe('state authority and terminal commands', () => {
  it('keeps old answerable question when a new draft advances and revises draft wording', () => {
    const old = { id: QUESTION, ordinal: 1, kind: 'bank' as const, text: 'First?', topic_id: 'api', source_answer_id: null, delivery_status: 'completed' as const }
    const draft = { ...old, id: NEXT, ordinal: 2, text: 'Draft?', delivery_status: 'pending' as const }
    const saved: TranscriptResponse = { ...transcript, questions: [{ ...old, source_quote: null, answer: null }] }
    const first = resolveQuestions(state({ current_question: draft, answerable_question_id: QUESTION }), saved)
    expect(first.answerableQuestion?.text).toBe('First?')
    expect(first.answerableQuestion?.delivery_status).toBeNull()
    expect(first.upcomingQuestion?.text).toBe('Draft?')
    expect(resolveQuestions(state({ current_question: { ...draft, text: 'Revised draft?' }, answerable_question_id: QUESTION }), saved).upcomingQuestion?.text).toBe('Revised draft?')
    expect(resolveQuestions(state({ current_question: draft, answerable_question_id: QUESTION }), null).isResolvingAnswerableQuestion).toBe(true)
  })

  it('honors equal-revision dirty invalidation and blocks older responses from rolling state back', async () => {
    const { controller, api } = setup()
    const old = deferred<InterviewState>()
    const newer = deferred<InterviewState>()
    api.getInterview.mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise)
    const opening = controller.open(ID)
    void controller.refresh()
    old.resolve(state({ revision: 1, answered_count: 1 }))
    await vi.waitFor(() => expect(api.getInterview).toHaveBeenCalledTimes(2))
    newer.resolve(state({ revision: 2, answered_count: 2 }))
    await opening
    expect(controller.getSnapshot().state?.answered_count).toBe(2)
    api.getInterview.mockResolvedValueOnce(state({ revision: 1, answered_count: 0 }))
    await controller.refresh()
    expect(controller.getSnapshot().state?.answered_count).toBe(2)
    controller.deactivate()
  })

  it('refetches durable answers when newly reported evidence cites an unavailable answer', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    const initialTranscriptGets = api.getTranscript.mock.calls.length
    api.getInterview.mockResolvedValueOnce(state({
      revision: 2, rubric: [{ topic_id: 'api', status: 'assessed', skill_ids: [],
        evidence: [{ answer_id: QUESTION, quote: 'a saved quote' }] }],
    }))
    await controller.refresh()
    await vi.waitFor(() => expect(api.getTranscript.mock.calls.length).toBeGreaterThan(initialTranscriptGets))
    expect(controller.getSnapshot().state?.rubric[0].evidence[0].answer_id).toBe(QUESTION)
    controller.deactivate()
  })

  it('an End mutation fences old GET; lost End never claims cancellation', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    const old = deferred<InterviewState>()
    api.getInterview.mockImplementationOnce(() => old.promise)
    const fetch = controller.refresh()
    api.endInterview.mockResolvedValueOnce(state({ status: 'completed', revision: 4 }))
    await controller.end()
    old.resolve(state({ revision: 3, status: 'active' }))
    await fetch
    expect(controller.getSnapshot().state?.status).toBe('completed')
    api.endInterview.mockRejectedValueOnce(new ApiError('timeout', 'Unknown outcome', null, undefined, true))
    await controller.end()
    expect(controller.getSnapshot().endingUnconfirmed).toBe(true)
    expect(controller.getSnapshot().state?.status).toBe('completed')
    controller.deactivate()
  })

  it('evaluation running retry 202 remains running and zero is a legitimate completed score', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    api.getEvaluation.mockResolvedValueOnce({ status: 'running', result: null, error_code: null, can_retry: true })
    await controller.loadEvaluation()
    api.retryEvaluation.mockResolvedValueOnce({ status: 202, data: { status: 'running', result: null, error_code: null, can_retry: false } })
    await controller.retryEvaluation()
    expect(api.retryEvaluation).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().evaluation?.status).toBe('running')
    api.getEvaluation.mockResolvedValueOnce({ status: 'completed', result: {
      overall_score: 0, rubric_scores: [{ topic_id: 'api', score: 0, answer_ids: [] }], strengths: [],
      concerns: [], insufficient_evidence: true, gaps: ['api'],
    }, error_code: null, can_retry: false })
    await controller.loadEvaluation()
    expect(controller.getSnapshot().evaluation?.result?.overall_score).toBe(0)
    controller.deactivate()
  })

  it('reconciles finished state after an explicit failed-evaluation retry succeeds', async () => {
    const { controller, api } = setup()
    api.getInterview.mockResolvedValue(state({
      status: 'evaluation_failed', revision: 2, evaluation_status: 'failed',
      last_error_code: 'evaluation_failed', can_resume: false,
    }))
    api.getEvaluation.mockResolvedValue({
      status: 'failed', result: null, error_code: 'evaluation_failed', can_retry: true,
    })
    await controller.open(ID)
    await controller.loadEvaluation()
    expect(controller.getSnapshot().evaluation?.can_retry).toBe(true)
    api.retryEvaluation.mockResolvedValueOnce({ status: 200, data: completedEvaluation })
    api.getInterview.mockResolvedValue(state({
      status: 'completed', revision: 3, evaluation_status: 'completed',
      last_error_code: null, can_resume: false,
    }))
    api.getEvaluation.mockResolvedValue(completedEvaluation)
    await controller.retryEvaluation()
    expect(api.retryEvaluation).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(controller.getSnapshot().state?.status).toBe('completed'))
    expect(controller.getSnapshot().evaluation?.result?.overall_score).toBe(0)
    expect(controller.getSnapshot().state?.last_error_code).toBeNull()
    controller.deactivate()
  })

  it('refetches finished interview state when evaluation resolves after media loss', async () => {
    const { controller, api } = setup()
    api.getInterview.mockResolvedValueOnce(state({
      status: 'evaluating', revision: 2, evaluation_status: 'running', can_resume: false,
    }))
    api.getEvaluation.mockResolvedValueOnce({
      status: 'running', result: null, error_code: null, can_retry: false,
    })
    await controller.open(ID)
    await controller.loadEvaluation()
    api.getEvaluation.mockResolvedValueOnce(completedEvaluation)
    api.getInterview.mockResolvedValueOnce(state({
      status: 'completed', revision: 3, evaluation_status: 'completed', can_resume: false,
    }))
    await controller.loadEvaluation()
    await vi.waitFor(() => expect(controller.getSnapshot().state?.status).toBe('completed'))
    expect(controller.getSnapshot().evaluation?.result?.overall_score).toBe(0)
    controller.deactivate()
  })

  it('completed evaluation without a result is an integration error, never a score', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    api.getEvaluation.mockResolvedValueOnce({ status: 'completed', result: null, error_code: null, can_retry: false })
    await controller.loadEvaluation()
    expect(controller.getSnapshot().evaluationError).toMatch(/missing its result/)
    expect(controller.getSnapshot().evaluation?.status).not.toBe('completed')
    controller.deactivate()
  })

  it('a 502 evaluation retry keeps saved work and refreshes the retryable envelope', async () => {
    const { controller, api } = setup()
    await controller.open(ID)
    api.getEvaluation.mockResolvedValueOnce({ status: 'running', result: null, error_code: null, can_retry: true })
    await controller.loadEvaluation()
    api.retryEvaluation.mockRejectedValueOnce(new ApiError('http', 'Evaluation service unavailable', 502))
    api.getEvaluation.mockResolvedValueOnce({ status: 'failed', result: null, error_code: 'evaluation_failed', can_retry: true })
    await controller.retryEvaluation()
    await vi.waitFor(() => expect(controller.getSnapshot().evaluation?.status).toBe('failed'))
    expect(controller.getSnapshot().transcript).not.toBeNull()
    expect(controller.getSnapshot().evaluation?.can_retry).toBe(true)
    controller.deactivate()
  })

  it('stops a paused voice run so the saved interview can be resumed deliberately', async () => {
    const { controller, api, rooms, tracks } = setup()
    await controller.open(ID)
    await controller.resume()
    api.getInterview.mockResolvedValueOnce(state({
      status: 'paused', revision: 2, last_error_code: 'llm_unavailable', can_resume: true,
    }))
    await controller.refresh()
    expect(tracks[0].stop).toHaveBeenCalled()
    expect(rooms[0].disconnect).toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ phase: 'disconnected', state: { status: 'paused' } })
    await controller.resume()
    expect(api.connectInterview).toHaveBeenCalledTimes(2)
    expect(rooms).toHaveLength(2)
    controller.deactivate()
  })

  it('keeps a paused recovery room until the worker resolves pending input or fails again', async () => {
    const { controller, api, rooms } = setup()
    api.getInterview.mockResolvedValueOnce(state({
      status: 'paused', revision: 2, last_error_code: 'llm_unavailable', can_resume: true,
    }))
    await controller.open(ID)
    api.connectInterview.mockResolvedValueOnce({
      status: 200, data: { ...grant, state: state({
        status: 'paused', revision: 3, last_error_code: null, can_resume: true,
      }) },
    })
    await controller.resume()
    api.getInterview.mockResolvedValueOnce(state({
      status: 'paused', revision: 3, last_error_code: null, can_resume: true,
    }))
    await controller.refresh()
    expect(rooms[0].disconnect).not.toHaveBeenCalled()
    expect(controller.getSnapshot().phase).toBe('connected')
    api.getInterview.mockResolvedValueOnce(state({
      status: 'paused', revision: 4, last_error_code: 'llm_unavailable', can_resume: true,
    }))
    await controller.refresh()
    expect(rooms[0].disconnect).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().phase).toBe('disconnected')
    controller.deactivate()
  })

  it('natural finish stops capture immediately and closes receive-only audio at 30 seconds without resetting the deadline', async () => {
    const { controller, api, rooms, tracks } = setup()
    await controller.open(ID)
    await controller.resume()
    api.getInterview.mockResolvedValueOnce(state({ status: 'evaluating', revision: 2 }))
    await controller.refresh()
    expect(tracks[0].stop).toHaveBeenCalled()
    expect(rooms[0].localParticipant.unpublishTrack).toHaveBeenCalledOnce()
    expect(rooms[0].disconnect).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(20_000)
    api.getInterview.mockResolvedValueOnce(state({ status: 'completed', revision: 3 }))
    await controller.refresh()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(rooms[0].disconnect).toHaveBeenCalled()
    expect(api.endInterview).not.toHaveBeenCalled()
    controller.deactivate()
  })
})
