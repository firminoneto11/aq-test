import { createContext } from 'react'
import type { InterviewController, InterviewViewState } from './controller'

export type InterviewContextValue = { view: InterviewViewState; controller: InterviewController }
export const InterviewContext = createContext<InterviewController | null>(null)
