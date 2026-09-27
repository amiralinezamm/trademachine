import { useEffect, useState } from 'react'
import { apiGet } from '../api/client'
import { AITextLoading } from '../components/AITextLoading'

interface GapEvent { ts_start: number; ts_end: number; duration_minutes: number; type: string }
interface ConnectionLog { events: GapEvent[]; total_gaps: number; note: string }
interface Resources { cpu_pct: number; ram_pct: number; disk_pct: number; load_avg: number[] }

function ts(epoch: number) {
  return new Date(epoch * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

function ResourceBar({ label, pct }: { label: string; pct: number }) {
  const color = pct > 85 ? '#ef5350' : pct > 65 ? '#ffb74d' : '#26a69a'
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8rem', marginBottom: 3, color: '#d1d4dc' }}>
        <span>{label}</span><span style={{ color }}>{pct.toFixed(1)}%</span>
      </div>
      <div style={{ height: 4, background: '#2a2e39', borderRadius: 2 }}>
        <div style={{ height: '100%', width: `${Math.min(pct, 100)}%`, background: color, borderRadius: 2, transition: 'width 0.4s' }} />
      </div>
    </div>
  )
}

export function Logs() {
  const [logs, setLogs] = useState<ConnectionLog | null>(null)
  const [res, setRes] = useState<Resources | null>(null)
  const [resError, setResError] = useState('')
  const [logsError, setLogsError] = useState('')
  const [loading, setLoading] = useState(true)

  // default: last 7 days
  const [from] = useState(() => Math.floor(Date.now() / 1000) - 7 * 86400)
  const [to] = useState(() => Math.floor(Date.now() / 1000))

  useEffect(() => {
    setLoading(true)
    Promise.allSettled([
      apiGet<ConnectionLog>(`/admin/api/logs/connection?from=${from}&to=${to}`),
      apiGet<Resources>('/admin/api/status/resources'),
    ]).then(([logsRes, resRes]) => {
      if (logsRes.status === 'fulfilled') setLogs(logsRes.value)
      else setLogsError('خطا در بارگذاری گزارش اتصال')
      if (resRes.status === 'fulfilled') setRes(resRes.value)
      else setResError('psutil در دسترس نیست')
      setLoading(false)
    })
  }, [from, to])

  if (loading) return <div style={{ height: '100%', background: '#131722' }}><AITextLoading text="در حال بارگذاری لاگ‌ها" /></div>

  return (
    <div style={c.page}>
      {/* Resources */}
      <div style={c.section}>
        <div style={c.title}>منابع سرور</div>
        {resError ? (
          <div style={{ color: '#787b86', fontSize: '0.8rem' }}>{resError}</div>
        ) : res ? (
          <div style={{ maxWidth: 360 }}>
            <ResourceBar label="CPU" pct={res.cpu_pct} />
            <ResourceBar label="RAM" pct={res.ram_pct} />
            <ResourceBar label="Disk" pct={res.disk_pct} />
            <div style={{ fontSize: '0.75rem', color: '#787b86' }}>
              Load avg: {res.load_avg.map(v => v.toFixed(2)).join(' / ')}
            </div>
          </div>
        ) : null}
      </div>

      {/* Connection gaps */}
      <div style={c.section}>
        <div style={c.title}>قطعی‌های اتصال (۷ روز اخیر)</div>
        {logsError ? (
          <div style={{ color: '#787b86', fontSize: '0.8rem' }}>{logsError}</div>
        ) : logs ? (
          <>
            {logs.note && (
              <div style={{ fontSize: '0.75rem', color: '#787b86', marginBottom: 8 }}>{logs.note}</div>
            )}
            <div style={{ fontSize: '0.8rem', color: '#787b86', marginBottom: 8 }}>
              {logs.total_gaps} قطعی شناسایی شد
            </div>
            {logs.events.length === 0 ? (
              <div style={{ color: '#26a69a', fontSize: '0.8rem' }}>بدون قطعی</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={c.table}>
                  <thead>
                    <tr>
                      {['شروع', 'پایان', 'مدت (دقیقه)', 'نوع'].map(h => (
                        <th key={h} style={c.th}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {logs.events.map((e, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid #1e222d' }}>
                        <td style={c.td}>{ts(e.ts_start)}</td>
                        <td style={c.td}>{ts(e.ts_end)}</td>
                        <td style={{ ...c.td, color: e.duration_minutes > 60 ? '#ef5350' : '#d1d4dc' }}>
                          {e.duration_minutes.toFixed(1)}
                        </td>
                        <td style={c.td}>{e.type}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}

const c: Record<string, React.CSSProperties> = {
  page: { padding: 16, overflowY: 'auto', height: '100%', boxSizing: 'border-box', color: '#d1d4dc', direction: 'rtl' },
  center: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#787b86' },
  section: { marginBottom: 24 },
  title: { fontSize: '0.8rem', color: '#787b86', marginBottom: 10, textTransform: 'uppercase', letterSpacing: 1 },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' },
  th: { padding: '6px 8px', textAlign: 'right', color: '#787b86', fontWeight: 400, borderBottom: '1px solid #2a2e39', whiteSpace: 'nowrap' },
  td: { padding: '5px 8px', color: '#d1d4dc', whiteSpace: 'nowrap' },
}
