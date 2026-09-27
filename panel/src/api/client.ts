// Dispatched when any authenticated request gets 401 (session expired)
// App.tsx listens to this to show the login form without a page reload.
export const UNAUTH_EVENT = 'panel:unauthorized'

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(path, { ...init, credentials: 'include' })
  if (res.status === 401 && !path.endsWith('/login')) {
    window.dispatchEvent(new CustomEvent(UNAUTH_EVENT))
    throw new Error('401')
  }
  return res
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await apiFetch(path)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export async function apiPost<T>(path: string, body: unknown): Promise<{ ok: true; data: T }> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({})) as { detail?: string }
    const err = new Error(data.detail ?? `HTTP ${res.status}`) as Error & { status: number }
    err.status = res.status
    throw err
  }
  return { ok: true, data: (await res.json()) as T }
}

export async function apiRequest<T>(path: string, method: 'PUT' | 'PATCH' | 'DELETE', body?: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({})) as { detail?: string }
    const err = new Error(data.detail ?? `HTTP ${res.status}`) as Error & { status: number }
    err.status = res.status
    throw err
  }
  return res.json() as Promise<T>
}
