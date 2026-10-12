export type TabState = {
  version: 1
  interviewId: string | null
  pendingCreate: { jobId: string; requestId: string } | null
  pendingConnect: { interviewId: string; requestId: string } | null
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
export const isUuid = (value: unknown): value is string => typeof value === 'string' && uuid.test(value)

const memory = new Map<string, TabState>()

function valid(value: unknown): value is TabState {
  if (typeof value !== 'object' || value === null) return false
  const state = value as Partial<TabState>
  return state.version === 1 && (state.interviewId === null || isUuid(state.interviewId)) &&
    (state.pendingCreate === null || (typeof state.pendingCreate === 'object' && state.pendingCreate !== null &&
      isUuid(state.pendingCreate.jobId) && isUuid(state.pendingCreate.requestId))) &&
    (state.pendingConnect === null || (typeof state.pendingConnect === 'object' && state.pendingConnect !== null &&
      isUuid(state.pendingConnect.interviewId) && isUuid(state.pendingConnect.requestId))) &&
    Object.hasOwn(state, 'pendingCreate') && Object.hasOwn(state, 'pendingConnect') && Object.hasOwn(state, 'interviewId')
}

export type TabStateStore = {
  getSnapshot(): TabState
  update(changes: Partial<Omit<TabState, 'version'>>): TabState
  readonly storageAvailable: boolean
}

export function createTabStateStore(ownerId: string): TabStateStore {
  if (!isUuid(ownerId)) throw new Error('Invalid account identity')
  const key = `aq-interviewer:v1:${ownerId}`
  let storage: Storage | null = null
  const fallback = memory.get(ownerId)
  let state: TabState = { version: 1, interviewId: null, pendingCreate: null, pendingConnect: null }
  let recovered = false
  let corrupt = false
  try {
    storage = sessionStorage
    const saved = storage.getItem(key)
    if (saved !== null) {
      try {
        const parsed: unknown = JSON.parse(saved)
        if (valid(parsed)) { state = parsed; recovered = true }
        else { corrupt = true; storage.removeItem(key) }
      } catch {
        corrupt = true
        storage.removeItem(key)
      }
    }
    memory.set(ownerId, state)
    storage.setItem(key, JSON.stringify(state))
  } catch {
    storage = null
    if (!recovered && !corrupt && fallback) state = fallback
    memory.set(ownerId, state)
  }
  return {
    getSnapshot: () => state,
    update(changes) {
      state = { ...state, ...changes }
      memory.set(ownerId, state)
      try {
        storage?.setItem(key, JSON.stringify(state))
      } catch {
        storage = null
      }
      return state
    },
    get storageAvailable() { return storage !== null },
  }
}

export function readTabState(ownerId: string): TabState {
  return createTabStateStore(ownerId).getSnapshot()
}
