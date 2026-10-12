import { describe, expect, it, vi } from 'vitest'
import { CaptionAccumulator } from './captions'

describe('Live caption previews', () => {
  it('appends deltas, replaces interim with final, ignores late interim, and resets per run', () => {
    const changed = vi.fn()
    const captions = new CaptionAccumulator(changed)
    captions.reset('run-one', 'candidate')
    const attrs = { 'lk.segment_id': 'segment', 'lk.transcribed_track_id': 'microphone' }
    captions.begin({ id: 'interim', participantIdentity: 'candidate', attributes: attrs })
    captions.append('interim', 'an')
    captions.append('interim', 'swer')
    expect(captions.getSnapshot()).toMatchObject([{ speaker: 'You', text: 'answer', final: false }])
    captions.begin({ id: 'final', participantIdentity: 'candidate', attributes: { ...attrs, 'lk.transcription_final': 'true' } })
    captions.append('final', 'corrected answer')
    captions.finish('final')
    captions.append('interim', ' late')
    expect(captions.getSnapshot()).toMatchObject([{ text: 'corrected answer', final: true }])
    captions.finish('interim')
    captions.reset('run-two', 'candidate')
    expect(captions.getSnapshot()).toEqual([])
    expect(changed).toHaveBeenCalled()
  })

  it('keeps only 100 completed segments and marks interrupted streams', () => {
    const captions = new CaptionAccumulator(() => {})
    captions.reset('run', 'candidate')
    for (let index = 0; index < 105; index++) {
      const id = String(index)
      captions.begin({ id, participantIdentity: 'interviewer' })
      captions.append(id, id)
      captions.finish(id, index === 104)
    }
    expect(captions.getSnapshot()).toHaveLength(100)
    expect(captions.getSnapshot().at(-1)).toMatchObject({ text: '104', speaker: 'Interviewer', interrupted: true })
  })
})
