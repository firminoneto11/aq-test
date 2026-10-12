export type Caption = Readonly<{
  key: string
  sequence: number
  speaker: 'You' | 'Interviewer'
  text: string
  final: boolean
  interrupted: boolean
}>

export type CaptionStream = {
  id: string
  participantIdentity: string
  attributes?: Record<string, string>
}

type StreamEntry = { key: string; final: boolean; text: string }

export class CaptionAccumulator {
  private runId: string | null = null
  private identity = ''
  private entries = new Map<string, Caption>()
  private streams = new Map<string, StreamEntry>()
  private sequence = 0
  private changed: () => void

  constructor(changed: () => void) { this.changed = changed }

  getSnapshot(): readonly Caption[] {
    return [...this.entries.values()].sort((a, b) => a.sequence - b.sequence)
  }

  reset(runId: string | null, ownIdentity = ''): void {
    this.runId = runId
    this.identity = ownIdentity
    this.entries.clear()
    this.streams.clear()
    this.sequence = 0
    this.changed()
  }

  begin(info: CaptionStream): void {
    if (!this.runId || this.streams.has(info.id)) return
    const attrs = info.attributes ?? {}
    const key = attrs['lk.segment_id'] && attrs['lk.transcribed_track_id']
      ? `${this.runId}:${info.participantIdentity}:${attrs['lk.transcribed_track_id']}:${attrs['lk.segment_id']}`
      : `${this.runId}:${info.participantIdentity}:${info.id}`
    const final = attrs['lk.transcription_final'] === 'true'
    this.streams.set(info.id, { key, final, text: '' })
    const previous = this.entries.get(key)
    if (!previous || (final && !previous.final)) {
      this.entries.set(key, { key, sequence: previous?.sequence ?? ++this.sequence,
        speaker: info.participantIdentity === this.identity ? 'You' : 'Interviewer',
        text: '', final, interrupted: false })
      this.changed()
    }
  }

  append(id: string, delta: string): void {
    const stream = this.streams.get(id)
    if (!stream) return
    stream.text += delta
    const current = this.entries.get(stream.key)
    if (!current || (current.final && !stream.final)) return
    this.entries.set(stream.key, { ...current, text: stream.text, interrupted: false })
    this.changed()
  }

  finish(id: string, interrupted = false): void {
    const stream = this.streams.get(id)
    if (!stream) return
    const current = this.entries.get(stream.key)
    if (current && (!current.final || stream.final)) {
      this.entries.set(stream.key, { ...current, interrupted })
      this.changed()
    }
    this.streams.delete(id)
    this.prune()
  }

  private prune(): void {
    const active = new Set([...this.streams.values()].map(entry => entry.key))
    const completed = [...this.entries.values()].filter(entry => !active.has(entry.key))
    for (const entry of completed.slice(0, Math.max(0, completed.length - 100))) this.entries.delete(entry.key)
    if (completed.length > 100) this.changed()
  }
}
