import { describe, expect, it } from 'vitest'
import { parseApiBaseUrl } from './config'

describe('API base configuration', () => {
  it('accepts absolute HTTP(S) /api/v1 bases and trims trailing slashes', () => {
    expect(parseApiBaseUrl(' http://localhost:8000/api/v1/// ')).toBe('http://localhost:8000/api/v1')
    expect(parseApiBaseUrl('https://api.example.test/review/api/v1')).toBe('https://api.example.test/review/api/v1')
  })

  it.each([
    undefined, '', '/api/v1', 'ftp://api.example.test/api/v1',
    'http://user:password@api.example.test/api/v1',
    'http://api.example.test/api/v1?token=secret',
    'http://api.example.test/api/v1#fragment',
    'http://api.example.test/api/v1?',
    'http://api.example.test/api/v1#',
    'http://api.example.test/jobs',
  ])('rejects unsafe or incomplete API base: %s', (value) => {
    expect(() => parseApiBaseUrl(value)).toThrow('Set VITE_API_BASE_URL')
  })
})
