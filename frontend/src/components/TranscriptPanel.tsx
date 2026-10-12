import { useEffect, useMemo, useRef, useState } from 'react'
import type { TranscriptResponse } from '../api/contracts'

type TranscriptPanelProps = {
  transcript: TranscriptResponse | null
  loading: boolean
  error: string | null
  onRetry: () => void
  citation: { answerId: string; quote: string | null } | null
}

function highlightedAnswer(text: string, quote: string | null) {
  if (!quote) return text
  const index = text.indexOf(quote)
  if (index < 0) return text
  return <>{text.slice(0, index)}<mark>{quote}</mark>{text.slice(index + quote.length)}</>
}

const messageKind = { input: 'Input', question: 'Question', answer: 'Answer', control: 'Spoken control', closing: 'Closing' } as const
const textSource = { stt: 'Speech transcription', tts_aligned: 'Aligned speech text', sdk_estimate: 'SDK estimate — not an exact recording of what was heard' } as const

export default function TranscriptPanel({ transcript, loading, error, onRetry, citation }: TranscriptPanelProps) {
  const [tab, setTab] = useState<'qa' | 'details'>('qa')
  const focusedCitation = useRef<TranscriptPanelProps['citation']>(null)
  const questions = useMemo(() => transcript ? [...transcript.questions].sort((a, b) => a.ordinal - b.ordinal) : [], [transcript])
  const messages = useMemo(() => transcript ? [...transcript.messages].sort((a, b) => a.sequence - b.sequence) : [], [transcript])
  useEffect(() => {
    if (!citation) { focusedCitation.current = null; return }
    if (focusedCitation.current === citation || !questions.some((question) => question.answer?.id === citation.answerId)) return
    if (tab !== 'qa') {
      const frame = requestAnimationFrame(() => setTab('qa'))
      return () => cancelAnimationFrame(frame)
    }
    const frame = requestAnimationFrame(() => {
      const item = document.getElementById(`saved-answer-${citation.answerId}`)
      if (!item) return
      item.focus()
      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      item.scrollIntoView({ behavior: reducedMotion ? 'instant' : 'smooth', block: 'center' })
      focusedCitation.current = citation
    })
    return () => cancelAnimationFrame(frame)
  }, [citation, questions, tab])

  return (
    <section className="card" aria-labelledby="transcript-heading">
      <h2 className="card__heading" id="transcript-heading">Saved interview record</h2>
      <p className="muted">These are saved questions, accepted answers and conversation details. Live captions are separate and may change.</p>
      <div className="tab-row" aria-label="Saved record views">
        <button type="button" className="button button--quiet" aria-pressed={tab === 'qa'} onClick={() => setTab('qa')}>Saved Q/A</button>
        <button type="button" className="button button--quiet" aria-pressed={tab === 'details'} onClick={() => setTab('details')}>Conversation details</button>
      </div>
      {loading && <p role="status">Loading saved record…</p>}
      {error && <div className="notice notice--error" role="alert">{error} <button type="button" className="button button--secondary" onClick={onRetry}>Retry saved record</button></div>}
      {!transcript && !loading && !error && <p>No saved record is available yet.</p>}
      {tab === 'qa' && transcript && (
        <ol className="qa-list">
          {questions.map((question) => (
            <li className="qa-item" key={question.id}>
              <h3>Question {question.ordinal}{question.kind === 'follow_up' ? ' · Follow-up' : ''}</h3>
              <p>{question.text}</p>
              {question.answer ? (
                <div id={`saved-answer-${question.answer.id}`} tabIndex={-1} aria-label={`Saved answer to question ${question.ordinal}`}>
                  <strong>Saved answer</strong>
                  <p>{highlightedAnswer(question.answer.text, citation?.answerId === question.answer.id ? citation.quote : null)}</p>
                </div>
              ) : <p className="muted">No accepted answer saved yet.</p>}
              {question.source_quote && <p className="muted">Follow-up source quote: “{question.source_quote}”</p>}
            </li>
          ))}
          {transcript.questions.length === 0 && <li>No approved questions have been saved yet.</li>}
        </ol>
      )}
      {tab === 'details' && transcript && (
        <ol className="message-list">
          {messages.map((message) => (
            <li key={message.id}>
              <p><strong>{message.speaker === 'user' ? 'You' : 'Interviewer'} · {messageKind[message.kind]}</strong></p>
              <p>{message.text}</p>
              <p className="muted">{message.processing_status === 'pending' ? 'Processing pending' : message.processing_status === 'applied' ? 'Processed' : 'Not applicable'} · {textSource[message.text_source]}{message.interrupted ? ' · Interrupted' : ''}</p>
            </li>
          ))}
          {transcript.messages.length === 0 && <li>No conversation messages saved yet.</li>}
        </ol>
      )}
    </section>
  )
}
