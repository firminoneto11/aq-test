import type { EvaluationResponse } from '../api/contracts'
import { errorGuidance, topicLabel } from './labels'

type EvaluationPanelProps = {
  evaluation: EvaluationResponse | null
  loading: boolean
  error: string | null
  onRetryEvaluation: () => void
  onRefreshEvaluation: () => void
  onCitation: (answerId: string, quote: string | null) => void
  retrying: boolean
}

function AnswerLinks({ ids, onCitation }: { ids: string[]; onCitation: EvaluationPanelProps['onCitation'] }) {
  if (ids.length === 0) return null
  return <span className="evidence-list">{' · '}{ids.map((id, index) => <span key={`${id}-${index}`}><button type="button" className="evidence-link" onClick={() => onCitation(id, null)}>Open source answer {index + 1}</button>{index < ids.length - 1 ? ' · ' : ''}</span>)}</span>
}

export default function EvaluationPanel({ evaluation, loading, error, onRetryEvaluation, onRefreshEvaluation, onCitation, retrying }: EvaluationPanelProps) {
  const result = evaluation?.status === 'completed' ? evaluation.result : null
  return (
    <section className="card" aria-labelledby="evaluation-heading">
      <h2 className="card__heading" id="evaluation-heading">Evaluation</h2>
      {loading && <p role="status">Loading evaluation…</p>}
      {error && <div className="notice notice--error" role="alert">{error} <button type="button" className="button button--secondary" onClick={onRefreshEvaluation}>Refresh evaluation</button></div>}
      {!evaluation && !loading && !error && <p>Evaluation is not available yet.</p>}
      {evaluation?.status === 'not_ready' && <p>Evaluation has not started. Saved answers are available below.</p>}
      {evaluation?.status === 'running' && <p role="status">Evaluation in progress. Your saved answers remain available while it runs.</p>}
      {evaluation?.status === 'failed' && <p role="alert">{errorGuidance(evaluation.error_code)}</p>}
      {evaluation?.status === 'completed' && !result && <p role="alert">The service reported a completed evaluation without a result. Refresh or contact the operator.</p>}
      {result && (
        <>
          <p className="score" aria-label={`Overall score ${result.overall_score} out of 100`}>{result.overall_score}<small> / 100</small></p>
          <p className="muted">Overall score returned by the service; not a hiring recommendation.</p>
          <h3>Rubric scores</h3>
          {result.rubric_scores.length ? <ul className="evidence-list">{result.rubric_scores.map((row, index) => <li className="rubric-row" key={`${row.topic_id}-${index}`}><strong>{topicLabel(row.topic_id)}</strong> · {row.score} / 4 <AnswerLinks ids={row.answer_ids} onCitation={onCitation} /></li>)}</ul> : <p>No rubric rows returned.</p>}
          <h3>Strengths</h3>
          {result.strengths.length ? <ul className="evidence-list">{result.strengths.map((item, index) => <li key={index}>{item.text}<AnswerLinks ids={item.answer_ids} onCitation={onCitation} /></li>)}</ul> : <p>No strengths returned.</p>}
          <h3>Concerns</h3>
          {result.concerns.length ? <ul className="evidence-list">{result.concerns.map((item, index) => <li key={index}>{item.text}<AnswerLinks ids={item.answer_ids} onCitation={onCitation} /></li>)}</ul> : <p>No concerns returned.</p>}
          {(result.insufficient_evidence || result.gaps.length > 0) && <div className="notice notice--warning notice--stack"><strong>Evidence limitations</strong>{result.insufficient_evidence && <p>Insufficient evidence was reported for some areas.</p>}{result.gaps.length > 0 && <ul>{result.gaps.map((gap, index) => <li key={index}>{topicLabel(gap)}</li>)}</ul>}</div>}
        </>
      )}
      {evaluation?.can_retry && evaluation.status !== 'completed' && (
        <button type="button" className="button button--secondary" disabled={retrying} onClick={onRetryEvaluation}>{retrying ? 'Retrying evaluation…' : 'Retry evaluation'}</button>
      )}
    </section>
  )
}
