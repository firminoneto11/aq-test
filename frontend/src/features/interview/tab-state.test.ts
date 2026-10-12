import { describe, expect, it, vi } from 'vitest'
import { createTabStateStore, readTabState } from './tab-state'

const OWNER = '8ae8a98e-3f88-4dca-a747-cc5288d73e45'
const OTHER = 'b55d1e21-ae7b-4804-b182-d077577b6475'
const REQUEST = '78f4d6e2-423d-4669-a824-e222576bfb07'

describe('owner-scoped tab intents', () => {
  it('keeps only safe identifiers and never reads another owner’s saved interview', () => {
    const storage = new Map<string, string>()
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value) },
      removeItem: (key: string) => { storage.delete(key) },
    })
    const store = createTabStateStore(OWNER)
    store.update({ pendingCreate: { jobId: OTHER, requestId: REQUEST } })
    expect(readTabState(OWNER).pendingCreate?.requestId).toBe(REQUEST)
    expect(readTabState(OTHER).pendingCreate).toBeNull()
    expect(storage.get(`aq-interviewer:v1:${OWNER}`)).not.toContain('token')
    vi.unstubAllGlobals()
  })

  it('resets a corrupt entry rather than trusting an obsolete in-memory copy', () => {
    const storage = new Map([[`aq-interviewer:v1:${OTHER}`, '{bad-json']])
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value) },
      removeItem: (key: string) => { storage.delete(key) },
    })
    expect(createTabStateStore(OTHER).getSnapshot().pendingConnect).toBeNull()
    expect(storage.get(`aq-interviewer:v1:${OTHER}`)).not.toContain('{bad-json')
    vi.unstubAllGlobals()
  })

  it('continues in memory when tab storage is unavailable', () => {
    vi.stubGlobal('sessionStorage', { getItem: () => { throw new Error('disabled') } })
    const store = createTabStateStore(OTHER)
    expect(store.storageAvailable).toBe(false)
    store.update({ pendingConnect: { interviewId: OWNER, requestId: REQUEST } })
    expect(readTabState(OTHER).pendingConnect?.requestId).toBe(REQUEST)
    vi.unstubAllGlobals()
  })
})
