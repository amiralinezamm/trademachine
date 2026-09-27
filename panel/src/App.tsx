import { useEffect, useState } from 'react'
import { Login } from './components/Login'
import { AITextLoading } from './components/AITextLoading'
import { TimeframeSwitch } from './components/TimeframeSwitch'
import { LayerPanel } from './components/LayerPanel'
import { ChartHost } from './chart/ChartHost'
import { ConnectionStatus } from './components/ConnectionStatus'
import { OnboardingTour } from './components/OnboardingTour'
import { Dashboard } from './pages/Dashboard'
import { Logs } from './pages/Logs'
import { useStore } from './state/store'
import { UNAUTH_EVENT, apiPost } from './api/client'

type AuthState = 'checking' | 'logged-in' | 'logged-out'
type Tab = 'chart' | 'dashboard' | 'logs'

export default function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [tab, setTab] = useState<Tab>('chart')
  const isTehran = useStore(s => s.isTehran)
  const toggleTimezone = useStore(s => s.toggleTimezone)

  useEffect(() => {
    let cancelled = false
    function tryCheck(attempt: number) {
      fetch('/admin/api/status', { credentials: 'include' })
        .then(r => {
          if (cancelled) return
          // Only treat 401 as logged-out. 5xx or transient errors keep session.
          if (r.status === 401) setAuth('logged-out')
          else setAuth('logged-in')
        })
        .catch(() => {
          if (cancelled) return
          // Service may be restarting — retry up to 3 times before giving up
          if (attempt < 3) setTimeout(() => tryCheck(attempt + 1), 2000)
          else setAuth('logged-out')
        })
    }
    tryCheck(0)
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const handler = () => setAuth('logged-out')
    window.addEventListener(UNAUTH_EVENT, handler)
    return () => window.removeEventListener(UNAUTH_EVENT, handler)
  }, [])

  async function logout() {
    await apiPost('/admin/api/logout', {}).catch(() => null)
    setAuth('logged-out')
  }

  if (auth === 'checking') {
    return (
      <div style={{ height: '100vh', background: '#131722' }}>
        <AITextLoading text="در حال احراز هویت" />
      </div>
    )
  }

  if (auth === 'logged-out') {
    return <Login onLogin={() => setAuth('logged-in')} />
  }

  const TABS: { id: Tab; label: string }[] = [
    { id: 'chart', label: 'نمودار' },
    { id: 'dashboard', label: 'داشبورد' },
    { id: 'logs', label: 'لاگ‌ها' },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '6px 12px',
        background: '#1e222d', borderBottom: '1px solid #2a2e39', flexShrink: 0,
      }}>
        <span style={{ color: '#d1d4dc', fontWeight: 700, fontSize: '0.95rem', letterSpacing: 1 }}>
          XAUUSD
        </span>
        {tab === 'chart' && <TimeframeSwitch />}
        <div style={{ flex: 1 }} />

        {/* Nav tabs */}
        <div style={{ display: 'flex', gap: 2 }}>
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                padding: '3px 10px', border: 'none', borderRadius: 4,
                background: tab === t.id ? '#2962ff' : 'transparent',
                color: tab === t.id ? '#fff' : '#787b86',
                cursor: 'pointer', fontSize: '0.8rem',
                transition: 'background 0.15s',
              }}
            >
              {t.label}
            </button>
          ))}
        </div>

        <ConnectionStatus />

        <button
          onClick={toggleTimezone}
          style={{
            padding: '3px 10px', border: '1px solid #2a2e39', borderRadius: 4,
            background: 'transparent', color: '#787b86', cursor: 'pointer', fontSize: '0.8rem',
          }}
        >
          {isTehran ? 'تهران' : 'UTC'}
        </button>
        <button
          onClick={logout}
          style={{
            padding: '3px 10px', border: '1px solid #2a2e39', borderRadius: 4,
            background: 'transparent', color: '#787b86', cursor: 'pointer', fontSize: '0.8rem',
          }}
        >
          خروج
        </button>
        <OnboardingTour />
      </div>

      {/* Page content */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {tab === 'chart' && (
          <>
            <ChartHost />
            <LayerPanel />
          </>
        )}
        {tab === 'dashboard' && <Dashboard />}
        {tab === 'logs' && <Logs />}
      </div>
    </div>
  )
}
