import { useEffect, useState } from 'react'
import { apiGet } from '../api/client'

interface StatusData {
  data: { lag_minutes: number | null }
}

const LAG_WARN = 5  // minutes

export function ConnectionStatus() {
  const [lag, setLag] = useState<number | null>(null)
  const [error, setError] = useState(false)

  async function check() {
    try {
      const d = await apiGet<StatusData>('/admin/api/status')
      setLag(d.data.lag_minutes)
      setError(false)
    } catch {
      setError(true)
    }
  }

  useEffect(() => {
    check()
    const id = setInterval(check, 30_000)
    return () => clearInterval(id)
  }, [])

  const isOk = !error && lag !== null && lag < LAG_WARN
  const color = isOk ? '#26a69a' : '#ef5350'
  const label = error ? 'قطع' : lag === null ? '...' : `${lag}m`

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.78rem', color: '#787b86' }}>
      <span
        style={{
          width: 8, height: 8, borderRadius: '50%',
          background: color,
          boxShadow: isOk ? `0 0 0 0 ${color}` : 'none',
          animation: isOk ? 'pulse 2s infinite' : 'none',
          flexShrink: 0,
        }}
      />
      <span style={{ color }}>{label}</span>
      <style>{`
        @keyframes pulse {
          0% { box-shadow: 0 0 0 0 rgba(38,166,154,0.6); }
          70% { box-shadow: 0 0 0 6px rgba(38,166,154,0); }
          100% { box-shadow: 0 0 0 0 rgba(38,166,154,0); }
        }
      `}</style>
    </div>
  )
}
