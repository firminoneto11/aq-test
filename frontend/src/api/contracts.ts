import { z } from 'zod'

export const uuidSchema = z.uuid()
const nonempty = z.string().min(1).refine((value) => value.trim().length > 0)
const nonnegativeInteger = z.number().int().nonnegative()

export const loginResponseSchema = z.object({
  access_token: z.string().min(1).regex(/^\S+$/),
  token_type: z.literal('bearer'),
  expires_in: z.number().int().positive(),
})

export const userSchema = z.object({ id: uuidSchema, email: z.email() })
export const jobSchema = z.object({
  id: uuidSchema,
  slug: nonempty,
  title: nonempty,
  description: z.string(),
  pack_version: nonnegativeInteger,
})
export const jobsResponseSchema = z.object({ jobs: z.array(jobSchema) })

export const interviewStatusSchema = z.enum([
  'created', 'active', 'paused', 'evaluating', 'completed', 'evaluation_failed', 'cancelled',
])
export const evaluationStatusSchema = z.enum(['not_ready', 'running', 'failed', 'completed'])
export const deliveryStatusSchema = z.enum(['pending', 'completed', 'interrupted', 'failed'])

export const evidenceSchema = z.object({ answer_id: uuidSchema, quote: z.string() })
export const rubricSignalSchema = z.object({
  topic_id: nonempty,
  status: z.enum(['unobserved', 'insufficient', 'assessed']),
  skill_ids: z.array(nonempty),
  evidence: z.array(evidenceSchema),
})
export const questionSummarySchema = z.object({
  id: uuidSchema,
  ordinal: z.number().int().positive(),
  kind: z.enum(['bank', 'follow_up']),
  text: nonempty,
  topic_id: nonempty,
  source_answer_id: uuidSchema.nullable(),
  delivery_status: deliveryStatusSchema,
})
export const decisionSchema = z.object({
  action: z.enum(['ask', 'finish', 'pause']),
  reason_code: nonempty,
  reason: z.string(),
  topic_id: nonempty.nullable(),
  source_answer_id: uuidSchema.nullable(),
})
export const interviewStateSchema = z.object({
  id: uuidSchema,
  job: jobSchema,
  status: interviewStatusSchema,
  revision: nonnegativeInteger,
  question_count: nonnegativeInteger,
  answered_count: nonnegativeInteger,
  follow_up_count: nonnegativeInteger,
  current_question: questionSummarySchema.nullable(),
  answerable_question_id: uuidSchema.nullable(),
  rubric: z.array(rubricSignalSchema),
  topics_covered: z.array(nonempty),
  gaps: z.array(z.string()),
  skills_detected: z.array(z.object({ skill_id: nonempty, evidence: z.array(evidenceSchema) })),
  decision: decisionSchema.nullable(),
  evaluation_status: evaluationStatusSchema,
  last_error_code: z.string().nullable(),
  can_resume: z.boolean(),
})

const offsetTimestamp = z.iso.datetime({ offset: true })
const livekitUrl = z.url().refine((value) => /^wss?:\/\//i.test(value), 'Expected a WebSocket URL')
export const connectionGrantSchema = z.object({
  interview_id: uuidSchema,
  run_id: uuidSchema,
  server_url: livekitUrl,
  room_name: nonempty,
  participant_identity: nonempty,
  participant_token: nonempty,
  expires_at: offsetTimestamp,
  state: interviewStateSchema,
})
export const connectionPendingSchema = z.object({
  interview_id: uuidSchema,
  run_id: uuidSchema,
  status: z.literal('provisioning'),
  retry_after_ms: z.number().int().min(1).max(60_000),
})

export const evaluationSchema = z.object({
  strengths: z.array(z.object({ text: z.string(), answer_ids: z.array(uuidSchema) })),
  concerns: z.array(z.object({ text: z.string(), answer_ids: z.array(uuidSchema) })),
  rubric_scores: z.array(z.object({
    topic_id: nonempty,
    score: z.number().int().min(0).max(4),
    answer_ids: z.array(uuidSchema),
  })),
  overall_score: z.number().int().min(0).max(100),
  insufficient_evidence: z.boolean(),
  gaps: z.array(z.string()),
})
export const evaluationResponseSchema = z.object({
  status: evaluationStatusSchema,
  result: evaluationSchema.nullable(),
  error_code: z.string().nullable(),
  can_retry: z.boolean(),
}).superRefine((value, context) => {
  if (value.status !== 'completed' || value.result === null) return
  const topics = value.result.rubric_scores.map((row) => row.topic_id)
  if (topics.length !== 4 || new Set(topics).size !== topics.length) {
    context.addIssue({ code: 'custom', path: ['result', 'rubric_scores'], message: 'Expected four distinct rubric topics' })
  }
})

export const transcriptMessageSchema = z.object({
  id: uuidSchema,
  sequence: nonnegativeInteger,
  run_id: uuidSchema,
  sdk_message_id: nonempty,
  speaker: z.enum(['user', 'assistant']),
  question_id: uuidSchema.nullable(),
  answer_id: uuidSchema.nullable(),
  kind: z.enum(['input', 'question', 'answer', 'control', 'closing']),
  processing_status: z.enum(['pending', 'applied', 'not_applicable']),
  text: z.string(),
  interrupted: z.boolean(),
  text_source: z.enum(['stt', 'tts_aligned', 'sdk_estimate']),
})
export const transcriptResponseSchema = z.object({
  interview_id: uuidSchema,
  status: interviewStatusSchema,
  questions: z.array(z.object({
    id: uuidSchema,
    ordinal: z.number().int().positive(),
    kind: z.enum(['bank', 'follow_up']),
    text: nonempty,
    topic_id: nonempty,
    source_answer_id: uuidSchema.nullable(),
    source_quote: z.string().nullable(),
    answer: z.object({ id: uuidSchema, text: z.string() }).nullable(),
  })),
  messages: z.array(transcriptMessageSchema),
})

export type LoginResponse = z.infer<typeof loginResponseSchema>
export type User = z.infer<typeof userSchema>
export type Job = z.infer<typeof jobSchema>
export type JobsResponse = z.infer<typeof jobsResponseSchema>
export type InterviewStatus = z.infer<typeof interviewStatusSchema>
export type EvaluationStatus = z.infer<typeof evaluationStatusSchema>
export type DeliveryStatus = z.infer<typeof deliveryStatusSchema>
export type Evidence = z.infer<typeof evidenceSchema>
export type RubricSignal = z.infer<typeof rubricSignalSchema>
export type QuestionSummary = z.infer<typeof questionSummarySchema>
export type InterviewState = z.infer<typeof interviewStateSchema>
export type ConnectionGrant = z.infer<typeof connectionGrantSchema>
export type ConnectionPending = z.infer<typeof connectionPendingSchema>
export type Evaluation = z.infer<typeof evaluationSchema>
export type EvaluationResponse = z.infer<typeof evaluationResponseSchema>
export type TranscriptMessage = z.infer<typeof transcriptMessageSchema>
export type TranscriptResponse = z.infer<typeof transcriptResponseSchema>
