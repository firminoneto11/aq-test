import { ConnectionState, Room, RoomEvent, Track, createLocalAudioTrack } from 'livekit-client'
import type { LocalAudioTrack, RemoteTrack } from 'livekit-client'
import { ApiError } from '../../api/client'
import type { ApiClient } from '../../api/client'
import type { ConnectionGrant, EvaluationResponse, InterviewState, TranscriptResponse } from '../../api/contracts'
import { CaptionAccumulator } from './captions'
import type { Caption } from './captions'
import { createTabStateStore, isUuid } from './tab-state'
import type { TabStateStore, TabState } from './tab-state'

export type Phase = 'idle' | 'loading' | 'preflight' | 'creating' | 'provisioning' | 'joining' | 'connected' | 'reconnecting' | 'disconnected' | 'closing' | 'error'
export type DisplayQuestion = Readonly<{
  id: string
  ordinal: number
  kind: 'bank' | 'follow_up'
  text: string
  topic_id: string
  source_answer_id: string | null
  delivery_status: NonNullable<InterviewState['current_question']>['delivery_status'] | null
}>
export type InterviewViewState = Readonly<{
  state: InterviewState | null
  transcript: TranscriptResponse | null
  evaluation: EvaluationResponse | null
  phase: Phase
  transport: ConnectionState
  muted: boolean
  outputBlocked: boolean
  speakers: readonly ('You' | 'Interviewer')[]
  captions: readonly Caption[]
  answerableQuestion: DisplayQuestion | null
  upcomingQuestion: DisplayQuestion | null
  isResolvingAnswerableQuestion: boolean
  loadingTranscript: boolean
  loadingEvaluation: boolean
  error: string | null
  connectionError: ApiError | null
  transcriptError: string | null
  evaluationError: string | null
  storageWarning: string | null
  pendingCreate: TabState['pendingCreate']
  pendingConnect: TabState['pendingConnect']
  interviewId: string | null
  runId: string | null
  endingUnconfirmed: boolean
  closingAudio: boolean
  unavailable: boolean
}>

// The seam lets Node tests simulate only the media boundary, never the HTTP/product backend.
export type MediaBoundary = {
  createTrack: typeof createLocalAudioTrack
  createRoom: () => Room
}
const browserMedia: MediaBoundary = { createTrack: createLocalAudioTrack, createRoom: () => new Room() }
const FINISHED: Partial<Record<InterviewState['status'], true>> = { evaluating: true, completed: true, evaluation_failed: true }
const TERMINAL: Partial<Record<InterviewState['status'], true>> = { ...FINISHED, cancelled: true }
const warning = 'Tab storage is unavailable. Keep this tab open: a reload may lose an uncertain request ID.'
type TimerHandle = number | NodeJS.Timeout

export function resolveQuestions(state: InterviewState | null, transcript: TranscriptResponse | null, remembered: ReadonlyMap<string, DisplayQuestion> = new Map()):
  Pick<InterviewViewState, 'answerableQuestion' | 'upcomingQuestion' | 'isResolvingAnswerableQuestion'> {
  if (!state) return { answerableQuestion: null, upcomingQuestion: null, isResolvingAnswerableQuestion: false }
  const current = state.current_question
  const fromSummary = current && { ...current, delivery_status: current.delivery_status }
  const answerableId = state.answerable_question_id
  const stored = answerableId ? transcript?.questions.find(question => question.id === answerableId) : undefined
  const answerable = answerableId === current?.id ? fromSummary
    : answerableId ? remembered.get(answerableId) ?? (stored && { ...stored, delivery_status: null }) : null
  return {
    answerableQuestion: answerable ?? null,
    upcomingQuestion: current && current.id !== answerableId ? fromSummary : null,
    isResolvingAnswerableQuestion: !!answerableId && !answerable,
  }
}

function hasAnswer(transcript: TranscriptResponse | null, answerId: string): boolean {
  if (!transcript) return false
  for (const question of transcript.questions) if (question.answer?.id === answerId) return true
  return false
}

function missingSourceAnswer(state: InterviewState, transcript: TranscriptResponse | null): string | null {
  for (const signal of state.rubric)
    for (const evidence of signal.evidence)
      if (!hasAnswer(transcript, evidence.answer_id)) return evidence.answer_id
  for (const skill of state.skills_detected)
    for (const evidence of skill.evidence)
      if (!hasAnswer(transcript, evidence.answer_id)) return evidence.answer_id
  const decisionSource = state.decision?.source_answer_id
  if (decisionSource && !hasAnswer(transcript, decisionSource)) return decisionSource
  const questionSource = state.current_question?.source_answer_id
  if (questionSource && !hasAnswer(transcript, questionSource)) return questionSource
  return null
}

function missingEvaluationSource(evaluation: EvaluationResponse, transcript: TranscriptResponse | null): string | null {
  const result = evaluation.result
  if (!result) return null
  for (const strength of result.strengths)
    for (const id of strength.answer_ids)
      if (!hasAnswer(transcript, id)) return id
  for (const concern of result.concerns)
    for (const id of concern.answer_ids)
      if (!hasAnswer(transcript, id)) return id
  for (const score of result.rubric_scores)
    for (const id of score.answer_ids)
      if (!hasAnswer(transcript, id)) return id
  return null
}

function safeError(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof DOMException && error.name === 'NotAllowedError') return 'Allow microphone access in your browser, then retry.'
  if (error instanceof DOMException && error.name === 'NotFoundError') return 'No microphone was found. Connect one, then retry.'
  if (error instanceof DOMException && (error.name === 'NotReadableError' || error.name === 'AbortError')) return 'The microphone is unavailable or busy. Check your device, then retry.'
  return 'The connection could not be completed. Check your device and service, then retry.'
}

export class InterviewController {
  private api: ApiClient
  private tab: TabStateStore | null = null
  private ownerId: string
  private media: MediaBoundary
  private snapshot: InterviewViewState
  private listeners = new Set<() => void>()
  private captions: CaptionAccumulator
  private generation = 0
  private active = false
  private busy = false
  private evaluationBusy = false
  private endBusy = false
  private interviewId: string | null = null
  private runId: string | null = null
  private room: Room | null = null
  private microphone: LocalAudioTrack | null = null
  private mount: HTMLElement | null = null
  private attached = new Map<RemoteTrack, HTMLMediaElement>()
  private requests = new Set<AbortController>()
  private timers = new Set<TimerHandle>()
  private stateTimer: TimerHandle | null = null
  private evaluationTimer: TimerHandle | null = null
  private closingTimer: TimerHandle | null = null
  private stateFetch: Promise<void> | null = null
  private stateDirty = false
  private stateSequence = 0
  private revisionFloor = -1
  private lastStateError = false
  private transcriptFetch: Promise<void> | null = null
  private transcriptDirty = false
  private transcriptBlocked = false
  private transcriptManualPending = false
  private evaluationFetch: Promise<void> | null = null
  private evaluationSequence = 0
  private questionCache = new Map<string, DisplayQuestion>()
  private lastMissingSource: string | null = null
  private lastMissingRevision = -1
  private lastMissingEvaluationSource: string | null = null
  private closingStarted = false
  private muteBusy = false
  private audioBusy = false

  constructor(api: ApiClient, ownerId: string, media: MediaBoundary = browserMedia) {
    this.api = api
    this.ownerId = ownerId
    this.media = media
    this.snapshot = {
      state: null, transcript: null, evaluation: null, phase: 'idle', transport: ConnectionState.Disconnected,
      muted: false, outputBlocked: false, speakers: [], captions: [], answerableQuestion: null,
      upcomingQuestion: null, isResolvingAnswerableQuestion: false, loadingTranscript: false,
      loadingEvaluation: false, error: null, connectionError: null, transcriptError: null,
      evaluationError: null, storageWarning: null,
      pendingCreate: null, pendingConnect: null,
      interviewId: null, runId: null, endingUnconfirmed: false, closingAudio: false, unavailable: false,
    }
    this.captions = new CaptionAccumulator(() => this.change({ captions: this.captions.getSnapshot() }))
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  getSnapshot = (): InterviewViewState => this.snapshot

  private change(patch: Partial<InterviewViewState>): void {
    this.snapshot = { ...this.snapshot, ...patch }
    for (const listener of this.listeners) listener()
  }
  private save(patch: Partial<Omit<TabState, 'version'>>): void {
    const tab = this.tab
    if (!tab) return
    const state = tab.update(patch)
    this.change({ pendingCreate: state.pendingCreate, pendingConnect: state.pendingConnect,
      storageWarning: tab.storageAvailable ? null : warning })
  }
  attachAudioMount = (element: HTMLElement | null): void => {
    if (element === this.mount) return
    for (const audio of this.attached.values()) audio.remove()
    this.mount = element
    if (element) for (const audio of this.attached.values()) element.appendChild(audio)
  }
  activate(): void {
    if (this.active) return
    if (!this.tab) this.tab = createTabStateStore(this.ownerId)
    const saved = this.tab.getSnapshot()
    this.change({ pendingCreate: saved.pendingCreate, pendingConnect: saved.pendingConnect,
      storageWarning: this.tab.storageAvailable ? null : warning })
    this.active = true
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', this.onVisibility)
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', this.leave)
      window.addEventListener('resize', this.onResize)
    }
  }
  deactivate(): void {
    if (!this.active) return
    this.active = false
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.onVisibility)
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', this.leave)
      window.removeEventListener('resize', this.onResize)
    }
    this.leave()
    this.change({ state: null, transcript: null, evaluation: null, captions: [], error: null })
  }
  private onVisibility = (): void => {
    if (document.visibilityState === 'visible') {
      void this.refresh()
      if (this.snapshot.evaluation?.status === 'running') void this.loadEvaluation()
    } else {
      this.clearPollingTimers()
    }
  }
  private onResize = (): void => {
    if (window.innerWidth < 1024 && (this.busy || this.room || this.microphone)) {
      this.leave()
      this.change({ error: 'Desktop width is required for voice. Widen your window, then deliberately Resume.' })
    }
  }
  private newRequest(): AbortController {
    const request = new AbortController()
    this.requests.add(request)
    return request
  }
  private doneRequest(request: AbortController): void { this.requests.delete(request) }
  private clearTimers(): void {
    for (const timer of this.timers) clearTimeout(timer)
    this.timers.clear()
    this.stateTimer = null
    this.evaluationTimer = null
    this.closingTimer = null
  }
  private later(ms: number, action: () => void): TimerHandle {
    const timer = setTimeout(() => { this.timers.delete(timer); action() }, ms)
    this.timers.add(timer)
    return timer
  }
  private sleep(ms: number, signal: AbortSignal): Promise<void> {
    const { promise, resolve, reject } = Promise.withResolvers<void>()
    if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return promise }
    const timer = setTimeout(() => { this.timers.delete(timer); signal.removeEventListener('abort', onAbort); resolve() }, ms)
    this.timers.add(timer)
    const onAbort = () => { clearTimeout(timer); this.timers.delete(timer); reject(new DOMException('Aborted', 'AbortError')) }
    signal.addEventListener('abort', onAbort, { once: true })
    return promise
  }
  private current(generation: number, id: string | null, run: string | null = null): boolean {
    return this.active && generation === this.generation && this.interviewId === id && (run === null || this.runId === run)
  }
  private releaseMedia(): void {
    if (this.closingTimer !== null) {
      clearTimeout(this.closingTimer)
      this.timers.delete(this.closingTimer)
      this.closingTimer = null
    }
    const microphone = this.microphone
    this.microphone = null
    microphone?.stop()
    for (const [track, element] of this.attached) {
      track.detach(element)
      element.remove()
    }
    this.attached.clear()
    const room = this.room
    this.room = null
    if (room) {
      room.unregisterTextStreamHandler('lk.transcription')
      room.removeAllListeners()
      void room.disconnect().catch(() => {})
    }
    this.runId = null
    this.closingStarted = false
    this.captions.reset(null)
    this.change({ muted: false, speakers: [], outputBlocked: false, transport: ConnectionState.Disconnected,
      phase: ['connected', 'reconnecting', 'joining'].includes(this.snapshot.phase) ? 'disconnected' : this.snapshot.phase,
      runId: null, closingAudio: false })
  }
  private fence(): number {
    this.generation++
    this.stateSequence++
    this.evaluationSequence++
    this.lastMissingEvaluationSource = null
    this.clearTimers()
    for (const request of this.requests) request.abort()
    this.requests.clear()
    this.stateFetch = null
    this.transcriptFetch = null
    this.evaluationFetch = null
    this.stateDirty = false
    this.transcriptDirty = false
    this.transcriptBlocked = false
    this.transcriptManualPending = false
    this.muteBusy = false
    this.audioBusy = false
    this.busy = false
    this.evaluationBusy = false
    this.endBusy = false
    this.releaseMedia()
    return this.generation
  }
  leave = (): void => {
    this.fence()
    this.change({ phase: 'disconnected', error: null, connectionError: null })
  }
  async open(id: string): Promise<void> {
    if (!isUuid(id)) return
    if (this.interviewId !== id) {
      this.fence()
      this.interviewId = id
      this.revisionFloor = -1
      this.lastMissingSource = null
      this.lastMissingRevision = -1
      this.questionCache.clear()
      this.change({ interviewId: id, state: null, transcript: null, evaluation: null,
        phase: 'loading', unavailable: false, error: null, connectionError: null,
        evaluationError: null, transcriptError: null, endingUnconfirmed: false })
    }
    await this.refresh()
    if (this.current(this.generation, id) && !this.snapshot.unavailable) {
      void this.loadTranscript(false)
      if (this.snapshot.state && FINISHED[this.snapshot.state.status]) void this.loadEvaluation()
    }
  }

  async start(jobId: string, onCreated: (id: string) => void): Promise<void> {
    if (this.busy || !this.active) return
    if (!isUuid(jobId)) { this.change({ phase: 'error', error: 'Select a valid role before starting.' }); return }
    const saved = this.tab!.getSnapshot().pendingCreate
    if (saved && saved.jobId !== jobId) {
      this.change({ phase: 'error', error: 'An earlier Start request is unresolved. Retry that role before selecting another.' })
      return
    }
    if (this.interviewId) {
      this.fence()
      this.interviewId = null
      this.revisionFloor = -1
      this.questionCache.clear()
      this.lastMissingSource = null
      this.lastMissingRevision = -1
      this.change({ interviewId: null, state: null, transcript: null, evaluation: null,
        phase: 'idle', unavailable: false, error: null, connectionError: null,
        evaluationError: null, transcriptError: null, endingUnconfirmed: false })
    }
    this.busy = true
    const generation = this.generation
    try {
      const track = await this.preflight(generation, this.interviewId)
      if (!track) return
      const intent = saved ?? { jobId, requestId: crypto.randomUUID() }
      if (!saved) this.save({ pendingCreate: intent })
      this.change({ phase: 'creating', error: null })
      const request = this.newRequest()
      let created: { status: 200 | 201; data: InterviewState }
      try { created = await this.api.createInterview(intent.jobId, intent.requestId, request.signal) }
      finally { this.doneRequest(request) }
      if (!this.current(generation, this.interviewId)) return
      const id = created.data.id
      if (!isUuid(id)) throw new Error('Invalid interview identity')
      this.interviewId = id
      this.save({ interviewId: id, pendingCreate: null })
      this.acceptState(created.data, true)
      this.change({ interviewId: id, phase: 'provisioning' })
      onCreated(id)
      if (!this.current(generation, id)) return
      await this.admit(generation, id, track)
    } catch (error) {
      if (this.current(generation, this.interviewId)) this.failure(error)
    } finally {
      if (generation === this.generation) this.busy = false
      if (this.snapshot.phase !== 'connected' && !this.room) {
        this.microphone?.stop()
        this.microphone = null
      }
    }
  }
  private async preflight(generation: number, id: string | null): Promise<LocalAudioTrack | null> {
    if (typeof window === 'undefined' || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Microphone requires a secure context and a supported browser. Use Chrome or Edge on localhost or HTTPS.')
    }
    if (window.innerWidth < 1024) throw new Error('Widen the window to desktop width before starting voice.')
    this.change({ phase: 'preflight', error: null, connectionError: null })
    const track = await this.media.createTrack({ echoCancellation: true, noiseSuppression: true, autoGainControl: true })
    if (!this.current(generation, id)) { track.stop(); return null }
    this.microphone = track
    const onEnded = () => {
      if (!this.current(generation, this.interviewId) || this.microphone !== track) return
      this.leave()
      this.change({ error: 'The microphone stopped. Check your device, then Resume deliberately.' })
      void this.refresh()
    }
    track.mediaStreamTrack.addEventListener('ended', onEnded, { once: true })
    return track
  }
  async resume(): Promise<void> { await this.connectAction(false) }
  async retryConnection(): Promise<void> { await this.connectAction(true) }
  private async connectAction(retry: boolean): Promise<void> {
    const id = this.interviewId
    if (!id || this.busy || !this.active || this.room) return
    const state = this.snapshot.state
    if (state && (TERMINAL[state.status] || state.status !== 'created' && !state.can_resume)) return
    this.busy = true
    const generation = this.generation
    try {
      if (retry || this.snapshot.connectionError) await this.refresh()
      if (!this.current(generation, id) || this.lastStateError || this.snapshot.unavailable || this.snapshot.state &&
        (TERMINAL[this.snapshot.state.status] || this.snapshot.state.status !== 'created' && !this.snapshot.state.can_resume)) return
      const track = await this.preflight(generation, id)
      if (track) await this.admit(generation, id, track)
    } catch (error) {
      if (this.current(generation, id)) this.failure(error)
    } finally {
      if (generation === this.generation) this.busy = false
      if (!this.room) { this.microphone?.stop(); this.microphone = null }
    }
  }
  private async admit(generation: number, id: string, track: LocalAudioTrack): Promise<void> {
    const pending = this.tab!.getSnapshot().pendingConnect
    const intent = pending?.interviewId === id ? pending : { interviewId: id, requestId: crypto.randomUUID() }
    if (intent !== pending) this.save({ pendingConnect: intent })
    const request = this.newRequest()
    try {
      this.change({ phase: 'provisioning', connectionError: null, error: null })
      for (;;) {
        const response = await this.api.connectInterview(id, intent.requestId, request.signal)
        if (!this.current(generation, id)) return
        if (response.status === 202) {
          if (response.data.interview_id !== id) throw new Error('Connection attempt belongs to another interview')
          this.runId = response.data.run_id
          this.change({ runId: this.runId, phase: 'provisioning' })
          await this.sleep(response.data.retry_after_ms, request.signal)
          continue
        }
        if (response.data.interview_id !== id || this.runId && response.data.run_id !== this.runId)
          throw new Error('Connection attempt changed unexpectedly')
        await this.join(generation, id, response.data, track)
        if (this.current(generation, id, response.data.run_id)) this.save({ pendingConnect: null })
        return
      }
    } catch (error) {
      if (this.microphone === track) {
        this.microphone = null
        track.stop()
      }
      if (this.current(generation, id)) {
        if (error instanceof ApiError && error.status === 409) {
          await this.refresh()
          if (!this.current(generation, id)) return
          if (this.lastStateError) {
            this.change({ error: 'Saved state could not be checked. Retry loading it before changing the connection attempt.' })
          } else if (error.message.toLowerCase().includes('connection attempt expired')) {
            this.save({ pendingConnect: null })
            this.change({ error: this.snapshot.state?.can_resume || this.snapshot.state?.status === 'created'
              ? 'Connection attempt expired. Resume deliberately to start a new run.'
              : 'Connection attempt expired. Saved state does not allow voice to resume.' })
          } else if (error.message.toLowerCase().includes('interview cannot be resumed')) {
            this.change({ error: 'Interview cannot be resumed. Review the saved state.' })
          } else this.change({ error: 'Connection was rejected. Review saved state and retry only after resolving the service error.' })
        } else if (error instanceof ApiError && error.status === 404) {
          this.change({ error: 'Interview unavailable or not owned by this account.', unavailable: true })
        } else if (error instanceof ApiError && (error.status === 429 || error.status === 503)) {
          this.change({ error: error.status === 429 ? 'Voice capacity reached. Retry manually later.' : 'Voice service unavailable. Retry manually later.' })
        } else this.change({ error: safeError(error) })
        this.change({ phase: 'error', connectionError: error instanceof ApiError ? error : null })
      }
    } finally {
      this.doneRequest(request)
      if (!this.room && this.microphone === track) {
        this.microphone = null
        track.stop()
      }
    }
  }
  private async join(generation: number, id: string, grant: ConnectionGrant, track: LocalAudioTrack): Promise<void> {
    if (Date.parse(grant.expires_at) <= Date.now()) throw new Error('The connection grant expired. Retry the same connection attempt.')
    this.acceptState(grant.state, true)
    if (TERMINAL[grant.state.status]) {
      this.save({ pendingConnect: null })
      this.change({ phase: 'disconnected' })
      return
    }
    const run = grant.run_id
    this.runId = run
    this.captions.reset(run, grant.participant_identity)
    const room = this.media.createRoom()
    this.room = room
    const current = () => this.current(generation, id, run) && this.room === room
    room.on(RoomEvent.TrackSubscribed, remote => {
      if (!current() || remote.kind !== Track.Kind.Audio || !this.mount) return
      const element = remote.attach()
      this.mount.appendChild(element)
      this.attached.set(remote, element)
      this.change({ outputBlocked: !room.canPlaybackAudio })
    })
    room.on(RoomEvent.TrackUnsubscribed, remote => {
      const element = this.attached.get(remote)
      if (element) { remote.detach(element); element.remove(); this.attached.delete(remote) }
    })
    room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
      if (current()) this.change({ outputBlocked: !room.canPlaybackAudio })
    })
    room.on(RoomEvent.ActiveSpeakersChanged, participants => {
      if (current()) this.change({ speakers: participants.map(person => person.identity === grant.participant_identity ? 'You' : 'Interviewer') })
    })
    room.on(RoomEvent.MediaDevicesError, () => {
      if (!current()) return
      this.leave()
      this.change({ error: 'The microphone device failed. Check your device, then Resume deliberately.' })
      void this.refresh()
    })
    room.on(RoomEvent.Reconnecting, () => {
      if (current()) this.change({ phase: 'reconnecting', transport: ConnectionState.Reconnecting })
    })
    room.on(RoomEvent.Reconnected, () => {
      if (!current()) return
      this.change({ phase: 'connected', transport: room.state })
      void this.refresh()
      void this.loadTranscript(false)
    })
    room.on(RoomEvent.Disconnected, () => {
      if (!current()) return
      this.fence()
      this.change({ phase: 'disconnected', error: 'Voice disconnected. Review saved progress before resuming.' })
      void this.refresh()
      void this.loadTranscript(false)
    })
    room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
      if (!current() || topic !== 'interview.state') return
      try {
        const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(payload))
        if (typeof value !== 'object' || value === null) return
        const hint = value as Record<string, unknown>
        if (hint.type === 'interview.state_changed' && hint.interview_id === id &&
          typeof hint.revision === 'number' && Number.isSafeInteger(hint.revision) && hint.revision >= 0) {
          void this.refresh()
          void this.loadTranscript(false)
        }
      } catch { /* Untrusted/malformed packets do not change domain state. */ }
    })
    room.registerTextStreamHandler('lk.transcription', (reader, participant) => {
      if (!current()) return
      const stream = { id: reader.info.id, participantIdentity: participant.identity, attributes: reader.info.attributes }
      this.captions.begin(stream)
      const request = this.newRequest()
      void (async () => {
        try {
          for await (const delta of reader.withAbortSignal(request.signal)) {
            if (!current()) return
            this.captions.append(stream.id, delta)
          }
          if (current()) this.captions.finish(stream.id)
        } catch {
          if (current()) this.captions.finish(stream.id, true)
        } finally { this.doneRequest(request) }
      })()
    })
    this.change({ phase: 'joining', runId: run, transport: ConnectionState.Connecting })
    try {
      await room.connect(grant.server_url, grant.participant_token)
      if (!current()) return
      await room.localParticipant.publishTrack(track, { source: Track.Source.Microphone })
      if (!current()) return
      this.change({ phase: 'connected', transport: room.state, muted: track.isMuted, outputBlocked: !room.canPlaybackAudio })
      void this.refresh()
      void this.loadTranscript(false)
    } catch (error) {
      if (current()) this.releaseMedia()
      throw error
    } finally {
      if (!current() && this.room !== room) {
        room.removeAllListeners()
        void room.disconnect().catch(() => {})
      }
    }
  }
  private failure(error: unknown): void {
    if (error instanceof DOMException && error.name === 'AbortError') return
    this.change({ phase: 'error', error: error instanceof Error && !(error instanceof ApiError) &&
      /Microphone requires|Widen the window|Invalid interview identity/.test(error.message) ? error.message : safeError(error),
      connectionError: error instanceof ApiError ? error : null })
    this.releaseMedia()
  }

  private acceptState(state: InterviewState, mutation = false): void {
    if (state.id !== this.interviewId || state.revision < this.revisionFloor) return
    if (mutation) this.stateSequence++
    this.revisionFloor = state.revision
    const previous = this.snapshot.state
    if (state.current_question) this.questionCache.set(state.current_question.id, { ...state.current_question })
    const questions = resolveQuestions(state, this.snapshot.transcript, this.questionCache)
    this.change({ state, ...questions,
      phase: this.snapshot.phase === 'loading' || this.snapshot.phase === 'error' && !this.room ? 'disconnected' : this.snapshot.phase,
      error: this.snapshot.phase === 'error' && !this.room ? null : this.snapshot.error,
      unavailable: false })
    if (this.tab?.getSnapshot().interviewId !== state.id) this.save({ interviewId: state.id })
    const missing = missingSourceAnswer(state, this.snapshot.transcript)
    if (!missing) this.lastMissingSource = null
    else if (this.lastMissingSource !== missing || this.lastMissingRevision !== state.revision) {
      this.lastMissingSource = missing
      this.lastMissingRevision = state.revision
      void this.loadTranscript(false)
    }
    if (state.answerable_question_id && questions.isResolvingAnswerableQuestion) void this.loadTranscript(false)
    if (!previous || previous.revision !== state.revision || previous.current_question?.delivery_status !== state.current_question?.delivery_status ||
      previous.answerable_question_id !== state.answerable_question_id || FINISHED[state.status] && !FINISHED[previous.status]) void this.loadTranscript(false)
    if (FINISHED[state.status]) {
      if (this.room && !this.closingStarted) this.finishMedia()
      void this.loadEvaluation()
    } else if (state.status === 'cancelled' && this.room) this.releaseMedia()
    if (!TERMINAL[state.status]) this.scheduleState()
    else this.clearPollingTimers()
  }
  private finishMedia(): void {
    if (this.closingStarted) return
    this.closingStarted = true
    const track = this.microphone
    this.microphone = null
    track?.stop()
    if (track && this.room) void this.room.localParticipant.unpublishTrack(track, false).catch(() => {})
    this.change({ closingAudio: true, muted: true })
    this.closingTimer = this.later(30_000, () => { this.closingTimer = null; this.closeAudio() })
  }
  closeAudio(): void { if (this.closingStarted) this.releaseMedia() }
  private clearPollingTimers(): void {
    if (this.stateTimer !== null) { clearTimeout(this.stateTimer); this.timers.delete(this.stateTimer); this.stateTimer = null }
    if (this.evaluationTimer !== null) { clearTimeout(this.evaluationTimer); this.timers.delete(this.evaluationTimer); this.evaluationTimer = null }
  }
  private scheduleState(): void {
    if (this.transcriptBlocked) return
    if (!this.active || !this.interviewId || typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    if (this.snapshot.state && !['created', 'active', 'paused'].includes(this.snapshot.state.status)) return
    if (this.stateTimer !== null) { clearTimeout(this.stateTimer); this.timers.delete(this.stateTimer) }
    this.stateTimer = this.later(2000, () => { this.stateTimer = null; void this.refresh() })
  }
  async refresh(): Promise<void> {
    const id = this.interviewId
    if (!id || !this.active) return
    if (this.stateFetch) { this.stateDirty = true; return this.stateFetch }
    const generation = this.generation
    const fetch = async () => {
      do {
        this.stateDirty = false
        const seq = ++this.stateSequence
        const request = this.newRequest()
        try {
          const state = await this.api.getInterview(id, request.signal)
          if (!this.current(generation, id)) return
          this.lastStateError = false
          if (seq === this.stateSequence) this.acceptState(state)
        } catch (error) {
          if (!this.current(generation, id)) return
          if (!(error instanceof DOMException && error.name === 'AbortError')) {
            this.lastStateError = true
            this.clearPollingTimers()
            this.change({ phase: 'error', error: error instanceof ApiError && error.status === 404
              ? 'Interview unavailable or not owned by this account.' : safeError(error),
              unavailable: error instanceof ApiError && error.status === 404 })
            return
          }
        } finally { this.doneRequest(request) }
      } while (this.stateDirty && this.current(generation, id))
      if (this.current(generation, id)) this.scheduleState()
    }
    const promise = fetch().finally(() => { if (this.stateFetch === promise) this.stateFetch = null })
    this.stateFetch = promise
    return promise
  }
  async loadTranscript(manual = true): Promise<void> {
    const id = this.interviewId
    if (!id || !this.active || !manual && this.transcriptBlocked) return
    if (manual) this.transcriptBlocked = false
    if (this.transcriptFetch) {
      this.transcriptDirty = true
      if (manual) this.transcriptManualPending = true
      return this.transcriptFetch
    }
    const generation = this.generation
    const fetch = async () => {
      do {
        this.transcriptDirty = false
        const request = this.newRequest()
        this.change({ loadingTranscript: true })
        try {
          const transcript = await this.api.getTranscript(id, request.signal)
          if (!this.current(generation, id)) return
          if (transcript.interview_id !== id) throw new Error('Transcript belongs to another interview')
          this.transcriptBlocked = false
          this.scheduleState()
          this.change({ transcript, transcriptError: null, ...resolveQuestions(this.snapshot.state, transcript, this.questionCache) })
          this.transcriptManualPending = false
        } catch (error) {
          if (this.current(generation, id) && !(error instanceof DOMException && error.name === 'AbortError')) {
            this.transcriptBlocked = true
            this.clearPollingTimers()
            this.change({ transcriptError: safeError(error) })
          }
          if (this.transcriptManualPending && this.current(generation, id)) {
            this.transcriptManualPending = false
            this.transcriptDirty = true
            continue
          }
          return
        } finally { this.doneRequest(request) }
      } while (this.transcriptDirty && this.current(generation, id))
    }
    const promise = fetch().finally(() => {
      if (this.transcriptFetch === promise) this.transcriptFetch = null
      if (this.current(generation, id)) this.change({ loadingTranscript: false })
    })
    this.transcriptFetch = promise
    return promise
  }
  private scheduleEvaluation(): void {
    if (this.snapshot.evaluation?.status !== 'running' || !this.active ||
      typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    if (this.evaluationTimer !== null) { clearTimeout(this.evaluationTimer); this.timers.delete(this.evaluationTimer) }
    this.evaluationTimer = this.later(2000, () => { this.evaluationTimer = null; void this.loadEvaluation() })
  }
  private refreshEvaluationSources(evaluation: EvaluationResponse): void {
    const missing = missingEvaluationSource(evaluation, this.snapshot.transcript)
    if (!missing) this.lastMissingEvaluationSource = null
    else if (missing !== this.lastMissingEvaluationSource) {
      this.lastMissingEvaluationSource = missing
      void this.loadTranscript(false)
    }
  }
  async loadEvaluation(): Promise<void> {
    const id = this.interviewId
    if (!id || !this.active || this.evaluationFetch) return this.evaluationFetch ?? undefined
    const generation = this.generation
    const sequence = this.evaluationSequence
    const request = this.newRequest()
    this.change({ loadingEvaluation: true })
    this.evaluationFetch = (async () => {
      try {
        const evaluation = await this.api.getEvaluation(id, request.signal)
        if (!this.current(generation, id) || sequence !== this.evaluationSequence) return
        if (evaluation.status === 'completed' && evaluation.result === null)
          throw new Error('Completed evaluation is missing its result')
        this.change({ evaluation, evaluationError: null })
        this.refreshEvaluationSources(evaluation)
        this.scheduleEvaluation()
      } catch (error) {
        if (this.current(generation, id) && sequence === this.evaluationSequence) {
          if (this.evaluationTimer !== null) {
            clearTimeout(this.evaluationTimer)
            this.timers.delete(this.evaluationTimer)
            this.evaluationTimer = null
          }
          if (!(error instanceof DOMException && error.name === 'AbortError'))
            this.change({ evaluationError: error instanceof Error && error.message === 'Completed evaluation is missing its result'
              ? error.message : safeError(error) })
        }
      } finally {
        this.doneRequest(request)
        if (this.current(generation, id) && sequence === this.evaluationSequence && !this.evaluationBusy)
          this.change({ loadingEvaluation: false })
      }
    })()
    const promise = this.evaluationFetch.finally(() => { if (this.evaluationFetch === promise) this.evaluationFetch = null })
    this.evaluationFetch = promise
    return promise
  }
  async retryEvaluation(): Promise<void> {
    const id = this.interviewId
    if (!id || this.evaluationBusy || !this.snapshot.evaluation?.can_retry || !this.active) return
    this.evaluationBusy = true
    const generation = this.generation
    const request = this.newRequest()
    this.change({ loadingEvaluation: true, evaluationError: null })
    try {
      const response = await this.api.retryEvaluation(id, request.signal)
      if (!this.current(generation, id)) return
      if (response.data.status === 'completed' && response.data.result === null)
        throw new Error('Completed evaluation is missing its result')
      this.evaluationSequence++
      this.lastMissingEvaluationSource = null
      this.change({ evaluation: response.data })
      this.refreshEvaluationSources(response.data)
      this.scheduleEvaluation()
    } catch (error) {
      if (!this.current(generation, id)) return
      this.change({ evaluationError: error instanceof Error && error.message === 'Completed evaluation is missing its result'
        ? error.message : safeError(error) })
      if (error instanceof ApiError && (error.status === 409 || error.status === 502)) {
        this.evaluationSequence++
        void this.refresh()
        const existing = this.evaluationFetch
        if (existing) void existing.then(() => { if (this.current(generation, id)) void this.loadEvaluation() })
        else void this.loadEvaluation()
      }
    } finally {
      this.doneRequest(request)
      if (this.current(generation, id)) { this.evaluationBusy = false; this.change({ loadingEvaluation: false }) }
    }
  }
  async end(): Promise<void> {
    const id = this.interviewId
    if (!id || !this.active || this.endBusy) return
    this.endBusy = true
    this.fence()
    this.endBusy = true
    const generation = this.generation
    this.change({ phase: 'closing', endingUnconfirmed: false, error: null })
    const request = this.newRequest()
    try {
      const state = await this.api.endInterview(id, request.signal)
      if (!this.current(generation, id)) return
      this.acceptState(state, true)
      this.change({ phase: 'disconnected', endingUnconfirmed: false })
      void this.loadTranscript(false)
    } catch (error) {
      if (this.current(generation, id)) this.change({ phase: 'error', endingUnconfirmed: true,
        error: `Ending was not confirmed. ${safeError(error)} Retry End after checking saved state.` })
    } finally {
      this.doneRequest(request)
      if (generation === this.generation) this.endBusy = false
    }
  }
  async setMuted(muted: boolean): Promise<void> {
    const track = this.microphone
    if (!track || !this.room || this.snapshot.phase !== 'connected' || this.muteBusy) return
    const generation = this.generation
    this.muteBusy = true
    try {
      if (muted) await track.mute()
      else await track.unmute()
      if (generation === this.generation && this.microphone === track) this.change({ muted: track.isMuted, error: null })
    } catch { if (generation === this.generation) this.change({ error: 'Unable to change microphone mute. Check your device.' }) }
    finally { if (generation === this.generation) this.muteBusy = false }
  }
  async enableAudio(): Promise<void> {
    const room = this.room
    if (!room || this.audioBusy) return
    const generation = this.generation
    this.audioBusy = true
    try {
      await room.startAudio()
      if (generation === this.generation && this.room === room)
        this.change({ outputBlocked: !room.canPlaybackAudio, error: room.canPlaybackAudio ? null : 'Audio remains blocked. Enable playback in your browser, then ask the interviewer to repeat the question.' })
    } catch {
      if (generation === this.generation) this.change({ outputBlocked: true, error: 'Audio could not be enabled. Check browser playback settings, then ask the interviewer to repeat the question.' })
    } finally { if (generation === this.generation) this.audioBusy = false }
  }
}
