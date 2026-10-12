import type { InterviewState } from '../api/contracts'

type VoiceControlsProps = {
  state: InterviewState
  phase: string
  transport: string
  muted: boolean
  outputBlocked: boolean
  retryPending: boolean
  endingUnconfirmed: boolean
  closingAudio: boolean
  busy: boolean
  onResume: () => void
  onRetryConnection: () => void
  onMute: (muted: boolean) => void
  onEnableAudio: () => void
  onLeave: () => void
  onEnd: () => void
  onCloseAudio: () => void
}

const busyPhases: Record<string, true> = { preflight: true, creating: true, provisioning: true, joining: true, closing: true }
const terminal: Record<string, true> = { evaluating: true, completed: true, evaluation_failed: true, cancelled: true }

export default function VoiceControls({ state, phase, transport, muted, outputBlocked, busy, retryPending, endingUnconfirmed, closingAudio, onResume, onRetryConnection, onMute, onEnableAudio, onLeave, onEnd, onCloseAudio }: VoiceControlsProps) {
  const joined = phase === 'connected' || phase === 'reconnecting'
  const isBusy = busy || !!busyPhases[phase]
  const mayConnect = (state.status === 'created' || state.can_resume) && !joined && !isBusy && !endingUnconfirmed && !terminal[state.status]
  const canRetry = mayConnect && retryPending
  return (
    <section className="card" aria-labelledby="voice-heading">
      <h2 className="card__heading" id="voice-heading">Voice and connection</h2>
      <p role="status">Connection: {phase === 'reconnecting' ? 'Reconnecting — waiting for the connection to recover' : joined ? 'Connected' : phase === 'provisioning' ? 'Preparing interviewer' : phase === 'joining' ? 'Joining voice session' : 'Not connected'} · Transport: {transport}</p>
      <p className="muted">Microphone: {joined && !terminal[state.status] ? muted ? 'Muted' : 'On' : 'Off'} · Interviewer audio: {outputBlocked ? 'Blocked' : joined ? 'Available' : 'Not connected'}</p>
      {outputBlocked && joined && <div className="notice notice--warning" role="alert">Interviewer audio is blocked. You may not have heard the question. Enable audio, then you can say “repeat the question”.</div>}
      {state.status === 'active' && !joined && <div className="notice notice--warning" role="status">No voice connection is owned in this tab. Resuming may take over another active connection; unsaved mid-sentence audio cannot be recovered.</div>}
      {state.status === 'paused' && <p>Saved progress is paused. Resuming may take over an existing connection; unsaved mid-sentence audio cannot be recovered.</p>}
      {state.status === 'created' && <p>Start voice when you are ready. Microphone permission is required before connecting.</p>}
      <div className="button-row">
        {joined && !terminal[state.status] && <button type="button" className="button button--secondary" onClick={() => onMute(!muted)}>{muted ? 'Unmute microphone' : 'Mute microphone'}</button>}
        {outputBlocked && joined && <button type="button" className="button" onClick={onEnableAudio}>Enable audio</button>}
        {mayConnect && <button type="button" className="button" onClick={canRetry ? onRetryConnection : onResume}>{canRetry ? 'Retry connection' : state.status === 'created' ? 'Start voice' : 'Resume interview'}</button>}
        {closingAudio && <button type="button" className="button button--secondary" onClick={onCloseAudio}>Close audio</button>}
        {!terminal[state.status] && <button type="button" className="button button--quiet" onClick={onLeave}>Leave for now</button>}
      </div>
      {!terminal[state.status] && <p className="muted">Switching tabs pauses saved-state updates, not the call. Leave for now stops local audio without ending this interview.</p>}
      {!terminal[state.status] && (
        <div className="end-action">
          <p>End interview saves an incomplete attempt if unfinished. It cannot be resumed. Leave for now keeps your saved progress instead.</p>
          <button type="button" className="button button--danger" disabled={phase === 'closing'} onClick={onEnd}>{endingUnconfirmed ? 'Retry ending interview' : 'End interview'}</button>
        </div>
      )}
    </section>
  )
}
