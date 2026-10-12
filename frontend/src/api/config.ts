const configurationMessage = 'Set VITE_API_BASE_URL to an absolute HTTP(S) URL ending in /api/v1 (for example, http://localhost:8000/api/v1).'

export function parseApiBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error(configurationMessage)
  const input = value.trim().replace(/\/+$/, '')
  let url: URL
  try {
    url = new URL(input)
  } catch {
    throw new Error(configurationMessage)
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    !url.hostname || url.username || url.password || url.search || url.hash || /[?#]/.test(input) ||
    !url.pathname.endsWith('/api/v1') ||
    url.pathname !== url.pathname.replace(/\/+$/, '') ||
    !/^https?:\/\//i.test(input)
  ) {
    throw new Error(configurationMessage)
  }
  return url.href.replace(/\/+$/, '')
}

export function readApiBaseUrl(): string {
  return parseApiBaseUrl(import.meta.env.VITE_API_BASE_URL)
}
