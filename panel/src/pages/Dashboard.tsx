import { useEffect, useState, useRef } from 'react'
import { apiGet, apiRequest } from '../api/client'
import { AITextLoading } from '../components/AITextLoading'

interface EquityPoint { ts: number; balance: number }
interface Components {
  level_kind?: string
  pattern?: { bullish?: string[]; bearish?: string[] }
  fib?: { overlapping?: boolean }
  gap?: { weight?: number }
  round?: { distance_atr?: number; multiplier?: number }
  regime?: string
}
interface SignalRow {
  id: number; ts: number; dir: string; entry: number | null
  sl: number | null; tp: number | null; conf: number | null
  outcome: string | null; pnl_usd: number | null; rule_version: string
  lot_size: number; components: Components | null
}
interface Summary {
  win_rate: number | null
  total_signals: number
  closed_signals: number
  open_signals: number
  total_pnl: number
  current_balance: number
  default_lot_size: number
  tracking_start: number | null
  equity_curve: EquityPoint[]
  last_signal: { id: number; ts: number; dir: string; entry: number | null; outcome: string | null; pnl_usd: number | null } | null
  signals: SignalRow[]
}

const OUTCOME_COLOR: Record<string, string> = {
  tp: '#26a69a',
  sl: '#ef5350',
  timeout: '#787b86',
  open: '#2962ff',
  level_invalidated: '#f57c00',
}
const OUTCOME_LABEL: Record<string, string> = {
  tp: 'TP',
  sl: 'SL',
  timeout: 'تایم‌اوت',
  open: 'باز',
  level_invalidated: 'سطح باطل',
}
const OUTCOME_ENTRIES = Object.entries(OUTCOME_COLOR)

function tsStr(epoch: number) {
  return new Date(epoch * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

function toLocalInput(epoch: number): string {
  const d = new Date(epoch * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function buildReason(c: Components | null): string {
  if (!c) return '—'
  const parts: string[] = []
  if (c.level_kind) parts.push(c.level_kind === 'support' ? 'حمایت' : 'مقاومت')
  const bullish = c.pattern?.bullish ?? []
  const bearish = c.pattern?.bearish ?? []
  if (bullish.length) parts.push(`${bullish[0]} ↑`)
  if (bearish.length) parts.push(`${bearish[0]} ↓`)
  if (c.fib?.overlapping) parts.push('فیبو')
  if (c.gap?.weight && c.gap.weight > 0.1) parts.push('گپ')
  if (c.round?.distance_atr != null && c.round.distance_atr < 0.5)
    parts.push(`رند×${c.round.multiplier ?? ''}`)
  return parts.join(' · ') || '—'
}

function MiniEquity({ points }: { points: EquityPoint[] }) {
  if (!points.length) return <div style={{ color: '#787b86', fontSize: '0.8rem' }}>بدون داده</div>
  const w = 400, h = 80
  const minB = Math.min(...points.map(p => p.balance))
  const maxB = Math.max(...points.map(p => p.balance))
  const range = maxB - minB || 1
  const pts = points.map((p, i) => {
    const x = (i / Math.max(points.length - 1, 1)) * w
    const y = h - ((p.balance - minB) / range) * (h - 8) - 4
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const last = points[points.length - 1]
  const isUp = last.balance >= points[0].balance
  return (
    <div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ display: 'block', height: h }}>
        <polyline points={pts} fill="none" stroke={isUp ? '#26a69a' : '#ef5350'} strokeWidth={1.5} />
      </svg>
      <div style={{ fontSize: '0.75rem', color: '#787b86', marginTop: 2 }}>
        {points[0] ? tsStr(points[0].ts) : ''} – {last ? tsStr(last.ts) : ''}
      </div>
    </div>
  )
}

function LotCell({ sig, onSave }: {
  sig: SignalRow
  onSave: (id: number, lot: number) => void
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(String(sig.lot_size))
  const [saving, setSaving] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function startEdit() {
    setVal(String(sig.lot_size))
    setEditing(true)
    setTimeout(() => inputRef.current?.select(), 10)
  }

  async function commit() {
    const n = parseFloat(val)
    if (isNaN(n) || n <= 0 || n > 100) { setEditing(false); return }
    setSaving(true)
    try {
      await apiRequest(`/admin/api/signals/${sig.id}/lot-size`, 'PATCH', { lot_size: n })
      onSave(sig.id, n)
    } catch {}
    setSaving(false)
    setEditing(false)
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={val}
        onChange={e => setVal(e.target.value)}
        onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setEditing(false) }}
        style={{ width: 60, background: '#2a2e39', border: '1px solid #2962ff', color: '#d1d4dc', borderRadius: 3, padding: '2px 4px', fontSize: '0.8rem' }}
        disabled={saving}
      />
    )
  }
  return (
    <span
      onClick={startEdit}
      style={{ cursor: 'pointer', color: '#d1d4dc', borderBottom: '1px dashed #2a2e39' }}
      title="کلیک برای ویرایش"
    >
      {sig.lot_size}
    </span>
  )
}

export function Dashboard() {
  const [data, setData] = useState<Summary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [defaultLotInput, setDefaultLotInput] = useState('')
  const [lotSaving, setLotSaving] = useState(false)
  const [trackingInput, setTrackingInput] = useState('')
  const [trackingSaving, setTrackingSaving] = useState(false)

  function load() {
    setLoading(true)
    apiGet<Summary>('/admin/api/dashboard/summary')
      .then(d => {
        setData(d)
        setDefaultLotInput(String(d.default_lot_size))
        if (d.tracking_start) setTrackingInput(toLocalInput(d.tracking_start))
        setLoading(false)
      })
      .catch(() => { setError('خطا در بارگذاری'); setLoading(false) })
  }

  useEffect(() => { load() }, [])

  async function saveDefaultLot() {
    const n = parseFloat(defaultLotInput)
    if (isNaN(n) || n <= 0 || n > 100) return
    setLotSaving(true)
    try {
      await apiRequest('/admin/api/settings/default-lot-size', 'PUT', { value: n })
      load()
    } catch {}
    setLotSaving(false)
  }

  async function saveTracking() {
    if (!trackingInput) return
    const epoch = Math.floor(new Date(trackingInput).getTime() / 1000)
    if (isNaN(epoch)) return
    setTrackingSaving(true)
    try {
      await apiRequest('/admin/api/settings/dashboard-tracking-start', 'PUT', { ts: epoch })
      load()
    } catch {}
    setTrackingSaving(false)
  }

  function handleLotSaved(id: number, lot: number) {
    setData(prev => {
      if (!prev) return prev
      const updated = prev.signals.map(s => s.id === id ? { ...s, lot_size: lot } : s)
      return { ...prev, signals: updated }
    })
    setTimeout(load, 400)
  }

  if (loading) return <div style={{ height: '100%', background: '#131722' }}><AITextLoading text="در حال بارگذاری داشبورد" /></div>
  if (error || !data) return <div style={s.center}>{error || 'خطا'}</div>

  const pnlColor = data.total_pnl >= 0 ? '#26a69a' : '#ef5350'

  return (
    <div style={s.page}>

      {/* Settings row */}
      <div style={s.settingsRow}>
        <div style={s.settingsGroup}>
          <span style={s.settingsLabel}>لات پیش‌فرض:</span>
          <input
            value={defaultLotInput}
            onChange={e => setDefaultLotInput(e.target.value)}
            style={s.smallInput}
            onKeyDown={e => { if (e.key === 'Enter') saveDefaultLot() }}
          />
          <button onClick={saveDefaultLot} disabled={lotSaving} style={s.smallBtn}>
            {lotSaving ? '...' : 'ذخیره'}
          </button>
        </div>
        <div style={s.settingsGroup}>
          <span style={s.settingsLabel}>شروع ردیابی:</span>
          <input
            type="datetime-local"
            value={trackingInput}
            onChange={e => setTrackingInput(e.target.value)}
            style={{ ...s.smallInput, width: 190 }}
          />
          <button onClick={saveTracking} disabled={trackingSaving} style={s.smallBtn}>
            {trackingSaving ? '...' : 'اعمال'}
          </button>
        </div>
      </div>

      {/* Stats cards */}
      <div style={s.cards}>
        <Card label="وین‌ریت" value={data.win_rate != null ? `${(data.win_rate * 100).toFixed(1)}%` : '—'} />
        <Card label="سیگنال‌ها" value={`${data.closed_signals} / ${data.total_signals}`} sub={`${data.open_signals} باز`} />
        <Card label="PnL کل" value={`$${data.total_pnl >= 0 ? '+' : ''}${data.total_pnl.toFixed(2)}`} color={pnlColor} />
        <Card label="موجودی" value={`$${data.current_balance.toFixed(2)}`} sub={`لات: ${data.default_lot_size}`} />
      </div>

      {/* Equity curve */}
      <div style={s.section}>
        <div style={s.sectionTitle}>منحنی سرمایه</div>
        <MiniEquity points={data.equity_curve} />
      </div>

      {/* Last signal */}
      {data.last_signal && (
        <div style={s.section}>
          <div style={s.sectionTitle}>آخرین سیگنال</div>
          <div style={{ ...s.lastSig, borderRight: `3px solid ${OUTCOME_COLOR[data.last_signal.outcome ?? 'open']}` }}>
            <span style={{ color: '#d1d4dc' }}>#{data.last_signal.id}</span>
            <span style={{ color: data.last_signal.dir === 'buy' ? '#26a69a' : '#ef5350' }}>
              {data.last_signal.dir?.toUpperCase()}
            </span>
            {data.last_signal.entry != null && <span>@ {data.last_signal.entry}</span>}
            <span style={{ color: OUTCOME_COLOR[data.last_signal.outcome ?? 'open'] }}>
              {OUTCOME_LABEL[data.last_signal.outcome ?? 'open'] ?? data.last_signal.outcome}
            </span>
            {data.last_signal.pnl_usd != null && (
              <span style={{ color: data.last_signal.pnl_usd >= 0 ? '#26a69a' : '#ef5350' }}>
                ${data.last_signal.pnl_usd >= 0 ? '+' : ''}{data.last_signal.pnl_usd.toFixed(2)}
              </span>
            )}
            <span style={{ color: '#787b86', fontSize: '0.75rem' }}>{tsStr(data.last_signal.ts)}</span>
          </div>
        </div>
      )}

      {/* Signals table */}
      <div style={s.section}>
        <div style={s.sectionTitle}>آخرین ۵۰ سیگنال</div>
        <div style={{ overflowX: 'auto' }}>
          <table style={s.table}>
            <thead>
              <tr>
                {['#', 'زمان', 'جهت', 'ورود', 'SL', 'TP', 'اطمینان', 'دلیل', 'نتیجه', 'PnL', 'لات'].map(h => (
                  <th key={h} style={s.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.signals.map((sig) => (
                <tr key={sig.id} style={{ borderBottom: '1px solid #1e222d' }}>
                  <td style={s.td}>{sig.id}</td>
                  <td style={{ ...s.td, color: '#787b86' }}>{tsStr(sig.ts)}</td>
                  <td style={{ ...s.td, color: sig.dir === 'buy' ? '#26a69a' : '#ef5350', fontWeight: 600 }}>
                    {sig.dir?.toUpperCase()}
                  </td>
                  <td style={s.td}>{sig.entry?.toFixed(2) ?? '—'}</td>
                  <td style={{ ...s.td, color: '#ef5350' }}>{sig.sl?.toFixed(2) ?? '—'}</td>
                  <td style={{ ...s.td, color: '#26a69a' }}>{sig.tp?.toFixed(2) ?? '—'}</td>
                  <td style={{ ...s.td, color: '#d1d4dc' }}>
                    {sig.conf != null ? `${(sig.conf * 100).toFixed(0)}%` : '—'}
                  </td>
                  <td style={{ ...s.td, color: '#787b86', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {buildReason(sig.components)}
                  </td>
                  <td style={{ ...s.td, color: OUTCOME_COLOR[sig.outcome ?? 'open'] }}>
                    {OUTCOME_LABEL[sig.outcome ?? 'open'] ?? sig.outcome}
                  </td>
                  <td style={{ ...s.td, color: (sig.pnl_usd ?? 0) >= 0 ? '#26a69a' : '#ef5350' }}>
                    {sig.pnl_usd != null ? `$${sig.pnl_usd >= 0 ? '+' : ''}${sig.pnl_usd.toFixed(2)}` : '—'}
                  </td>
                  <td style={s.td}>
                    <LotCell sig={sig} onSave={handleLotSaved} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Legend */}
        <div style={s.legend}>
          {OUTCOME_ENTRIES.map(([key, color]) => (
            <div key={key} style={s.legendItem}>
              <div style={{ width: 10, height: 10, borderRadius: '50%', background: color, flexShrink: 0 }} />
              <span style={{ color: '#787b86', fontSize: '0.75rem' }}>{OUTCOME_LABEL[key] ?? key}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={s.card}>
      <div style={{ fontSize: '0.75rem', color: '#787b86', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: '1.3rem', fontWeight: 700, color: color ?? '#d1d4dc' }}>{value}</div>
      {sub && <div style={{ fontSize: '0.72rem', color: '#787b86', marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

const s: Record<string, React.CSSProperties> = {
  page: {
    padding: 16,
    overflowY: 'auto',
    height: '100%',
    width: '100%',
    minWidth: 0,
    boxSizing: 'border-box',
    color: '#d1d4dc',
    direction: 'rtl',
    background: '#131722',
  },
  center: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#787b86' },
  cards: { display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 20 },
  card: { background: '#1e222d', border: '1px solid #2a2e39', borderRadius: 6, padding: '12px 16px', minWidth: 120, flex: '1 1 120px' },
  section: { marginBottom: 20 },
  sectionTitle: { fontSize: '0.75rem', color: '#787b86', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 1 },
  lastSig: { background: '#1e222d', borderRadius: 4, padding: '8px 12px', display: 'flex', gap: 12, alignItems: 'center', fontSize: '0.85rem', flexWrap: 'wrap' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' },
  th: { padding: '6px 8px', textAlign: 'right', color: '#787b86', fontWeight: 400, borderBottom: '1px solid #2a2e39', whiteSpace: 'nowrap' },
  td: { padding: '5px 8px', color: '#d1d4dc', whiteSpace: 'nowrap' },
  legend: { display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 10, paddingTop: 8, borderTop: '1px solid #1e222d' },
  legendItem: { display: 'flex', alignItems: 'center', gap: 5 },
  settingsRow: { display: 'flex', gap: 24, flexWrap: 'wrap', marginBottom: 16, padding: '10px 14px', background: '#1e222d', borderRadius: 6, border: '1px solid #2a2e39', alignItems: 'center' },
  settingsGroup: { display: 'flex', alignItems: 'center', gap: 8 },
  settingsLabel: { fontSize: '0.78rem', color: '#787b86', whiteSpace: 'nowrap' },
  smallInput: { background: '#131722', border: '1px solid #2a2e39', color: '#d1d4dc', borderRadius: 4, padding: '4px 8px', fontSize: '0.8rem', width: 100 },
  smallBtn: { background: '#2962ff', border: 'none', color: '#fff', borderRadius: 4, padding: '4px 12px', fontSize: '0.8rem', cursor: 'pointer' },
}
