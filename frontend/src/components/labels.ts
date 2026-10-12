export function topicLabel(value: string) {
  return value.split(/[_.-]/g).map((part) => {
    if (part.toLowerCase() === 'api' || part.toLowerCase() === 'sql') return part.toUpperCase()
    return part.charAt(0).toUpperCase() + part.slice(1)
  }).join(' ')
}

export function errorGuidance(code: string | null) {
  switch (code) {
    case 'llm_unavailable': return 'The assessment service is unavailable. Your saved progress remains available.'
    case 'llm_invalid_output': return 'The assessment service could not produce a usable response. Your saved progress remains available.'
    case 'stt_unavailable': return 'Speech transcription is unavailable. Check the voice service before resuming.'
    case 'tts_unavailable': return 'Interviewer audio is unavailable. Check the voice service before resuming.'
    case 'voice_dispatch_failed': return 'The voice interviewer could not start. Try connecting again when the service recovers.'
    case 'join_timeout': return 'The voice session timed out before it could start. Resume when you are ready.'
    case 'evaluation_failed': return 'Evaluation failed. Your saved answers remain available.'
    case 'lease_lost': return 'The voice session was interrupted. Review saved progress before resuming.'
    default: return 'The service could not complete this step. Your saved progress remains available.'
  }
}
