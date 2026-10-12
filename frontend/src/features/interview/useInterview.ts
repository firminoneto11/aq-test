import { useContext, useSyncExternalStore } from 'react'
import { InterviewContext } from './context'
import type { InterviewContextValue } from './context'

export function useInterview(): InterviewContextValue {
  const controller = useContext(InterviewContext)
  if (!controller) throw new Error('useInterview must be used inside InterviewProvider')
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  return { view, controller }
}
