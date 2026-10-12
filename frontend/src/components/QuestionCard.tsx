import type { QuestionSummary } from '../api/contracts'

type DisplayQuestion = Pick<QuestionSummary, 'id' | 'text' | 'kind' | 'ordinal'> & {
  delivery_status?: QuestionSummary['delivery_status'] | null
}
type QuestionCardProps = {
  answerable: DisplayQuestion | null
  upcoming: DisplayQuestion | null
  resolving: boolean
  waitingForInterviewer: boolean
}

const deliveryLabels = {
  pending: 'Awaiting delivery',
  completed: 'Delivery completed',
  interrupted: 'Delivery interrupted — the question may be repeated',
  failed: 'Delivery failed — the question may be repeated',
} as const

export default function QuestionCard({ answerable, upcoming, resolving, waitingForInterviewer }: QuestionCardProps) {
  return (
    <section className="card" aria-labelledby="question-heading">
      <h2 className="card__heading" id="question-heading">Current question</h2>
      {answerable ? (
        <>
          <p className="question-text">{answerable.text}</p>
          <p className="muted">Question {answerable.ordinal}{answerable.kind === 'follow_up' ? ' · Follow-up' : ''}</p>
          {answerable.delivery_status && <p className="muted">{deliveryLabels[answerable.delivery_status]}</p>}
        </>
      ) : resolving ? (
        <p role="status">Loading current question…</p>
      ) : upcoming ? (
        <p>Waiting for this question to begin.</p>
      ) : (
        <p>{waitingForInterviewer ? 'Waiting for interviewer.' : 'No question is currently being answered.'}</p>
      )}
      {upcoming && (
        <div className="upcoming">
          <h3>{answerable ? 'Up next — not yet begun' : 'Planned question — awaiting delivery'}</h3>
          <p>{upcoming.text}</p>
        </div>
      )}
    </section>
  )
}
