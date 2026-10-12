import { useCallback, useEffect, useMemo } from 'react'
import type { ReactNode } from 'react'
import type { ApiClient } from '../../api/client'
import { useAuth } from '../../auth/auth'
import { InterviewController } from './controller'
import { InterviewContext } from './context'

export function InterviewProvider({ children, api, ownerId }: { children: ReactNode; api: ApiClient; ownerId: string }) {
  const { registerTeardown } = useAuth()
  const controller = useMemo(() => new InterviewController(api, ownerId), [api, ownerId])
  const attachAudioMount = useCallback((element: HTMLDivElement | null) => {
    controller.attachAudioMount(element)
  }, [controller])
  useEffect(() => {
    controller.activate()
    const unregister = registerTeardown(controller.leave)
    return () => {
      unregister()
      controller.deactivate()
    }
  }, [controller, registerTeardown])
  return (
    <InterviewContext.Provider value={controller}>
      {children}
      <div ref={attachAudioMount} aria-hidden="true" className="interview-audio-mount" />
    </InterviewContext.Provider>
  )
}
