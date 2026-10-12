import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import AppShell from '../components/AppShell'
import StatusNotice from '../components/StatusNotice'
import QuestionCard from '../components/QuestionCard'
import EvidencePanel from '../components/EvidencePanel'
import TranscriptPanel from '../components/TranscriptPanel'
import VoiceControls from '../components/VoiceControls'
import EvaluationPanel from '../components/EvaluationPanel'
import { errorGuidance } from '../components/labels'
import { useInterview } from '../features/interview/useInterview'
import { isUuid } from '../features/interview/tab-state'

type Citation = { interviewId: string; answerId: string; quote: string | null }

export default function InterviewPage() {
  const { interviewId } = useParams()
  const navigate = useNavigate()
  const { view, controller } = useInterview()
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null)
  const [missingSource, setMissingSource] = useState<{ interviewId: string; answerId: string } | null>(null)
  const cleanup = useRef<number | undefined>(undefined)
  const citationRequest = useRef(0)
  const id = isUuid(interviewId) ? interviewId : null
  const citation = selectedCitation?.interviewId === id ? selectedCitation : null
  const missingAnswerId = missingSource?.interviewId === id ? missingSource.answerId : null

  useEffect(() => {
    window.clearTimeout(cleanup.current)
    cleanup.current = undefined
    citationRequest.current++
    if (id) void controller.open(id)
    else controller.leave()
    return () => {
      // StrictMode replays this effect without navigating. Only leave after an actual unmount.
      cleanup.current = window.setTimeout(() => controller.leave(), 0)
    }
  }, [controller, id])

  if (!id) {
    return <AppShell title="Interview not found"><StatusNotice tone="warning" action={<Link to="/jobs">Back to roles</Link>}>This address does not contain a valid interview ID. No interview was requested.</StatusNotice></AppShell>
  }

  const belongsHere = view.interviewId === id
  const state = belongsHere && !view.unavailable && view.state?.id === id ? view.state : null
  const transcript = belongsHere && view.transcript?.interview_id === id ? view.transcript : null
  const evaluation = belongsHere ? view.evaluation : null
  const showResults = state?.status === 'evaluating' || state?.status === 'completed' || state?.status === 'evaluation_failed'
  const showCaptions = state?.status === 'active' || state?.status === 'created' || state?.status === 'paused'
  const onCitation = (answerId: string, quote: string | null) => {
    const request = ++citationRequest.current
    setSelectedCitation({ interviewId: id, answerId, quote })
    setMissingSource(null)
    if (transcript?.questions.some((question) => question.answer?.id === answerId)) return
    void controller.loadTranscript().then(() => {
      if (request !== citationRequest.current || controller.getSnapshot().interviewId !== id) return
      const saved = controller.getSnapshot().transcript?.questions.some((question) => question.answer?.id === answerId)
      if (!saved) setMissingSource({ interviewId: id, answerId })
    }).catch(() => {
      if (request === citationRequest.current && controller.getSnapshot().interviewId === id) setMissingSource({ interviewId: id, answerId })
    })
  }
  const retrySource = () => {
    if (citation) onCitation(citation.answerId, citation.quote)
  }
  const leave = () => {
    controller.leave()
    navigate('/jobs')
  }

  return (
    <AppShell title={state ? state.job.title : 'Interview'} eyebrow="AI Interviewer" wide>
      {!state && (
        <StatusNotice tone={belongsHere && view.unavailable ? 'warning' : belongsHere && view.error ? 'error' : 'info'} action={belongsHere && view.error && !view.unavailable ? <button className="button button--secondary" type="button" onClick={() => void controller.open(id)}>Retry loading</button> : <Link to="/jobs">Back to roles</Link>}>
          {belongsHere && view.unavailable ? 'Interview unavailable or not owned by this account.' : belongsHere && view.error ? view.error : 'Loading saved interview…'}
        </StatusNotice>
      )}
      {state && (
        <div className="workspace">
          <div className="workspace__main">
            <section className="card" aria-label="Interview progress">
              <div className="progress"><span>{state.job.title}</span><span className={`badge ${state.status === 'active' ? 'badge--teal' : 'badge--warning'}`}>{state.status.replaceAll('_', ' ')}</span></div>
              <p>Answered {state.answered_count} · 6–10 questions</p>
              <p className="muted">{state.follow_up_count} answered follow-ups · {state.question_count} approved questions so far</p>
            </section>
            {view.storageWarning && <StatusNotice tone="warning">{view.storageWarning}</StatusNotice>}
            {state.last_error_code && <StatusNotice tone="warning">{errorGuidance(state.last_error_code)}</StatusNotice>}
            {belongsHere && view.error && <StatusNotice tone="error" title={view.endingUnconfirmed ? 'Ending was not confirmed' : 'Interview needs attention'} action={view.endingUnconfirmed ? <button type="button" className="button button--secondary" onClick={() => void controller.refresh()}>Check saved state</button> : undefined}>{view.error}</StatusNotice>}
            {state.status === 'cancelled' && <StatusNotice tone="warning" title="Incomplete interview">This attempt ended before natural completion. Saved answers below remain available; there is no final grade or resume action.</StatusNotice>}
            {state.status === 'evaluating' && <StatusNotice>Interview complete. Evaluation is in progress; saved Q/A is available below.</StatusNotice>}
            {(state.status === 'created' || state.status === 'active' || state.status === 'paused') && (
              <QuestionCard answerable={view.answerableQuestion} upcoming={view.upcomingQuestion} resolving={view.isResolvingAnswerableQuestion} waitingForInterviewer={view.phase === 'connected'} />
            )}
            {showCaptions && (
              <section className="card" aria-labelledby="captions-heading">
                <h2 className="card__heading" id="captions-heading">Live captions — may change before saving</h2>
                {view.speakers.length > 0 && <p className="muted">Speaking now: {view.speakers.join(', ')}</p>}
                {view.captions.length === 0 ? <p className="empty">Waiting for captions. Spoken answers are saved separately.</p> : (
                  <ol className="caption-list" aria-label="Live captions">
                    {view.captions.map((caption) => <li key={caption.key}><strong>{caption.speaker}{caption.interrupted ? ' · Interrupted' : caption.final ? '' : ' · Interim'}</strong><p>{caption.text || 'Listening…'}</p></li>)}
                  </ol>
                )}
              </section>
            )}
            {showResults && <EvaluationPanel evaluation={evaluation} loading={view.loadingEvaluation} error={view.evaluationError} retrying={view.loadingEvaluation} onCitation={onCitation} onRetryEvaluation={() => void controller.retryEvaluation()} onRefreshEvaluation={() => void controller.loadEvaluation()} />}
            <TranscriptPanel transcript={transcript} loading={view.loadingTranscript} error={view.transcriptError} onRetry={() => void controller.loadTranscript()} citation={citation} />
            {state.status !== 'cancelled' && <VoiceControls state={state} phase={view.phase} transport={String(view.transport)} muted={view.muted} outputBlocked={view.outputBlocked} busy={view.phase === 'closing'} retryPending={view.pendingConnect?.interviewId === id} endingUnconfirmed={view.endingUnconfirmed} closingAudio={view.closingAudio} onResume={() => void controller.resume()} onRetryConnection={() => void controller.retryConnection()} onMute={(muted) => void controller.setMuted(muted)} onEnableAudio={() => void controller.enableAudio()} onLeave={leave} onEnd={() => void controller.end()} onCloseAudio={() => controller.closeAudio()} />}
            {state.status === 'cancelled' && <Link className="button button--secondary" to="/jobs">Back to roles</Link>}
          </div>
          <div className="workspace__aside">
            <EvidencePanel state={state} transcript={transcript} loadingTranscript={view.loadingTranscript} onCitation={onCitation} missingAnswerId={missingAnswerId} onRetrySource={retrySource} />
          </div>
        </div>
      )}
    </AppShell>
  )
}
