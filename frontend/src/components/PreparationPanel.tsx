import type { Job } from '../api/contracts'
import StatusNotice from './StatusNotice'

type PreparationPanelProps = {
  job: Job
  pending: boolean
  onStart: () => void
  retrying?: boolean
  error?: string | null
}

export default function PreparationPanel({ job, pending, onStart, retrying = false, error }: PreparationPanelProps) {
  return (
    <section className="card stack" aria-labelledby="prepare-title">
      <div>
        <p className="eyebrow">Before you begin</p>
        <h2 id="prepare-title">Get ready for {job.title}</h2>
        <p className="lead">This English-language interview is spoken, not typed. Expect 6–10 questions including at least two follow-ups about your answers.</p>
      </div>
      <ul className="prep-list">
        <li>Use a desktop Chrome or Edge window, a working microphone, and audible speakers or headphones.</li>
        <li>Allow microphone access before the app requests a voice slot. If playback is blocked, use Enable audio.</li>
        <li>You can say “repeat the question” or “end the interview.” Leaving pauses an unfinished interview; ending saves an incomplete attempt that cannot resume.</li>
        <li>Accepted answers and the final evaluation are saved. This app does not store raw audio or video; external provider retention depends on those providers.</li>
      </ul>
      {error && <StatusNotice tone="error">{error}</StatusNotice>}
      <div className="button-row">
        <button className="button" type="button" disabled={pending} onClick={onStart}>
          {pending ? 'Preparing microphone…' : retrying ? 'Retry starting interview' : 'Start interview'}
        </button>
        <span className="muted">Microphone access is requested after you click Start.</span>
      </div>
    </section>
  )
}
