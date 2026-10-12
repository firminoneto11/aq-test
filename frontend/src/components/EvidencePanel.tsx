import type { Evidence, InterviewState, TranscriptResponse } from '../api/contracts'
import { topicLabel } from './labels'

type EvidencePanelProps = {
  state: InterviewState
  transcript: TranscriptResponse | null
  loadingTranscript: boolean
  onCitation: (answerId: string, quote: string | null) => void
  missingAnswerId: string | null
  onRetrySource: () => void
}

function Citation({ evidence, transcript, loading, onCitation }: {
  evidence: Evidence
  transcript: TranscriptResponse | null
  loading: boolean
  onCitation: EvidencePanelProps['onCitation']
}) {
  const saved = transcript?.questions.some((question) => question.answer?.id === evidence.answer_id)
  return (
    <li>
      <button type="button" className="evidence-link" onClick={() => onCitation(evidence.answer_id, evidence.quote)}>
        {evidence.quote ? `“${evidence.quote}” — open saved answer` : 'Open saved answer'}
      </button>
      {!saved && <span className="muted"> · {loading ? 'Source answer loading' : 'Source answer not yet loaded'}</span>}
    </li>
  )
}

export default function EvidencePanel({ state, transcript, loadingTranscript, onCitation, missingAnswerId, onRetrySource }: EvidencePanelProps) {
  const hasEvidence = state.rubric.length > 0 || state.topics_covered.length > 0 || state.gaps.length > 0 || state.skills_detected.length > 0 || state.decision !== null
  const sourceAnswerId = state.decision?.source_answer_id
  return (
    <aside className="card evidence-panel" aria-labelledby="evidence-heading">
      <h2 className="card__heading" id="evidence-heading">Model-extracted evidence</h2>
      <p className="muted">These are the service’s observations, not a hiring verdict.</p>
      {!hasEvidence && <p>No assessment has been returned yet. Evidence appears as saved answers are assessed.</p>}
      {state.decision && (
        <section className="evidence-topic">
          <h3>Why this question</h3>
          <p>{state.decision.reason}</p>
          {state.decision.topic_id && <p className="muted">Topic: {topicLabel(state.decision.topic_id)}</p>}
          {sourceAnswerId && (
            <button type="button" className="evidence-link" onClick={() => onCitation(sourceAnswerId, null)}>Open source answer</button>
          )}
        </section>
      )}
      {state.topics_covered.length > 0 && (
        <section className="evidence-topic"><h3>Topics covered</h3><ul className="evidence-list">{state.topics_covered.map((topic) => <li key={topic}>{topicLabel(topic)}</li>)}</ul></section>
      )}
      {state.rubric.length > 0 && (
        <section className="evidence-topic">
          <h3>Topic signals</h3>
          {state.rubric.map((signal) => (
            <div className="evidence-topic" key={signal.topic_id}>
              <h4>{topicLabel(signal.topic_id)}</h4>
              <p>{signal.status === 'unobserved' ? 'Not yet observed' : signal.status === 'insufficient' ? 'More evidence needed' : 'Assessed'}</p>
              {signal.skill_ids.length > 0 && <p className="muted">Skills: {signal.skill_ids.map(topicLabel).join(', ')}</p>}
              {signal.evidence.length > 0 && <ul className="evidence-list">{signal.evidence.map((item, index) => <Citation key={`${item.answer_id}-${index}`} evidence={item} transcript={transcript} loading={loadingTranscript} onCitation={onCitation} />)}</ul>}
            </div>
          ))}
        </section>
      )}
      {state.gaps.length > 0 && <section className="evidence-topic"><h3>Evidence still needed</h3><ul className="evidence-list">{state.gaps.map((gap, index) => <li key={index}>{topicLabel(gap)}</li>)}</ul></section>}
      {state.skills_detected.length > 0 && (
        <section className="evidence-topic"><h3>Supported skills</h3>{state.skills_detected.map((skill) => (
          <div key={skill.skill_id}><h4>{topicLabel(skill.skill_id)}</h4>
            {skill.evidence.length ? <ul className="evidence-list">{skill.evidence.map((item, index) => <Citation key={`${item.answer_id}-${index}`} evidence={item} transcript={transcript} loading={loadingTranscript} onCitation={onCitation} />)}</ul> : <p className="muted">No source answer cited.</p>}
          </div>
        ))}</section>
      )}
      {missingAnswerId && (
        <div className="notice notice--warning" role="status">Source answer unavailable. <button type="button" className="button button--secondary" onClick={onRetrySource}>Retry loading source</button></div>
      )}
    </aside>
  )
}
