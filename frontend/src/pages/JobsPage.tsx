import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import type { Job } from '../api/contracts'
import { useAuth } from '../auth/auth'
import AppShell from '../components/AppShell'
import PreparationPanel from '../components/PreparationPanel'
import StatusNotice from '../components/StatusNotice'
import { readTabState } from '../features/interview/tab-state'
import { useInterview } from '../features/interview/useInterview'

export default function JobsPage() {
  const { api, user } = useAuth()
  const { view, controller } = useInterview()
  const navigate = useNavigate()
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [retry, setRetry] = useState(0)
  const [startError, setStartError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const starting = useRef(false)
  const pendingJobId = view.pendingCreate?.jobId ?? null
  const selectedJob = jobs?.find((job) => job.id === (pendingJobId ?? selected))
  const savedInterview = user ? readTabState(user.id).interviewId : null

  useEffect(() => {
    if (!api) return
    const abort = new AbortController()
    void api.getJobs(abort.signal).then((result) => {
      setJobs(result.jobs)
    }).catch(() => {
      if (!abort.signal.aborted) setLoadError(true)
    })
    return () => abort.abort()
  }, [api, retry])

  async function start() {
    if (starting.current || !selectedJob) return
    starting.current = true
    setBusy(true)
    setStartError(null)
    try {
      await controller.start(selectedJob.id, (id) => navigate(`/interviews/${id}`))
    } catch {
      setStartError('Starting the interview was not confirmed. Retry using the same request, or open your saved interview.')
    } finally {
      starting.current = false
      setBusy(false)
    }
  }

  return (
    <AppShell title="Choose your interview" eyebrow="Your next step" wide>
      <div className="stack--large">
        <p className="lead">Choose a role, prepare your microphone, and practice in a real-time voice interview. Your interviewer will choose follow-ups from your answers.</p>
        {view.storageWarning && <StatusNotice tone="warning">{view.storageWarning}</StatusNotice>}
        {savedInterview && (
          <StatusNotice title="Continue your saved interview" action={<Link className="button button--secondary" to={`/interviews/${savedInterview}`}>Open interview</Link>}>
            Your latest interview from this tab is available after sign-in. Open it to review progress or deliberately resume if permitted.
          </StatusNotice>
        )}
        {loadError && <StatusNotice tone="error" action={<button className="button button--secondary" type="button" onClick={() => { setLoadError(false); setJobs(null); setRetry((count) => count + 1) }}>Retry</button>}>The job catalogue is unavailable. Check the API connection.</StatusNotice>}
        {!jobs && !loadError && <StatusNotice>Loading available roles…</StatusNotice>}
        {jobs?.length === 0 && <StatusNotice tone="warning" title="No roles are available">Ask the operator to seed the three interview job packs.</StatusNotice>}
        {jobs && jobs.length > 0 && (
          <>
            <div className="job-list" role="group" aria-label="Available interview roles">
              {jobs.map((job) => (
                <button
                  className="job-card" key={job.id} type="button"
                  aria-pressed={job.id === (pendingJobId ?? selected)}
                  disabled={Boolean(pendingJobId && pendingJobId !== job.id) || busy}
                  onClick={() => setSelected(job.id)}
                >
                  <span className="job-card__top"><strong>{job.title}</strong><span className="job-card__radio" aria-hidden="true" /></span>
                  <span className="job-card__desc">{job.description}</span>
                </button>
              ))}
            </div>
            {selectedJob && <PreparationPanel job={selectedJob} pending={busy} retrying={Boolean(pendingJobId)} error={view.error ?? startError} onStart={() => { void start() }} />}
            {pendingJobId && !selectedJob && <StatusNotice tone="warning">The role attached to an unfinished creation attempt is no longer in the catalogue. Do not start another job under that request ID; ask the operator to restore the role.</StatusNotice>}
          </>
        )}
      </div>
    </AppShell>
  )
}
