import { useStore, LayerState } from '../state/store'

interface LayerDef {
  key: keyof LayerState
  label: string
  alwaysOn?: boolean
}

const LAYERS: LayerDef[] = [
  { key: 'candles', label: 'کندل', alwaysOn: true },
  { key: 'levels', label: 'سطوح' },
  { key: 'signals', label: 'سیگنال‌ها' },
  { key: 'gaps', label: 'گپ‌ها' },
  { key: 'rounds', label: 'اعداد رند' },
  { key: 'fib', label: 'فیبوناچی' },
  { key: 'news', label: 'اخبار' },
  { key: 'patterns', label: 'الگوها' },
  { key: 'regime', label: 'رژیم' },
]

const LEVEL_STATUSES = ['active', 'broken', 'flipped', 'expired']
const SIGNAL_OUTCOMES = ['tp', 'sl', 'timeout', 'open', 'level_invalidated']
const OUTCOME_LABELS: Record<string, string> = {
  tp: 'TP', sl: 'SL', timeout: 'تایم‌اوت', open: 'باز', level_invalidated: 'سطح باطل',
}
const STATUS_LABELS: Record<string, string> = {
  active: 'فعال', broken: 'شکسته', flipped: 'برگشت', expired: 'منقضی',
}

const sep: React.CSSProperties = {
  borderTop: '1px solid #2a2e39', marginTop: 6, paddingTop: 6,
}

export function LayerPanel() {
  const layers = useStore(s => s.layers)
  const setLayer = useStore(s => s.setLayer)
  const levelStatusFilter = useStore(s => s.levelStatusFilter)
  const setLevelStatusFilter = useStore(s => s.setLevelStatusFilter)
  const signalOutcomeFilter = useStore(s => s.signalOutcomeFilter)
  const setSignalOutcomeFilter = useStore(s => s.setSignalOutcomeFilter)

  function toggleStatus(s: string) {
    setLevelStatusFilter(
      levelStatusFilter.includes(s)
        ? levelStatusFilter.filter(x => x !== s)
        : [...levelStatusFilter, s]
    )
  }

  function toggleOutcome(o: string) {
    setSignalOutcomeFilter(
      signalOutcomeFilter.includes(o)
        ? signalOutcomeFilter.filter(x => x !== o)
        : [...signalOutcomeFilter, o]
    )
  }

  return (
    <div style={{
      width: 160, background: '#1e222d', borderLeft: '1px solid #2a2e39',
      padding: '0.75rem', display: 'flex', flexDirection: 'column', gap: 4,
      overflowY: 'auto', flexShrink: 0,
    }}>
      <div style={{ color: '#787b86', fontSize: '0.75rem', marginBottom: 6, fontWeight: 600 }}>
        لایه‌ها
      </div>

      {LAYERS.map(({ key, label, alwaysOn }) => {
        const checked = layers[key]
        return (
          <label key={key} style={{
            display: 'flex', alignItems: 'center', gap: 8,
            cursor: alwaysOn ? 'default' : 'pointer',
            color: checked ? '#d1d4dc' : '#555',
            fontSize: '0.85rem',
          }}>
            <input
              type="checkbox"
              checked={checked}
              disabled={alwaysOn}
              onChange={e => setLayer(key, e.target.checked)}
              style={{ accentColor: '#2962ff' }}
            />
            {label}
          </label>
        )
      })}

      {/* Level status filter */}
      {layers.levels && (
        <div style={sep}>
          <div style={{ color: '#787b86', fontSize: '0.7rem', marginBottom: 4, fontWeight: 600 }}>
            وضعیت سطح
          </div>
          {LEVEL_STATUSES.map(s => (
            <label key={s} style={{
              display: 'flex', alignItems: 'center', gap: 6,
              cursor: 'pointer',
              color: levelStatusFilter.includes(s) ? '#d1d4dc' : '#555',
              fontSize: '0.78rem',
            }}>
              <input
                type="checkbox"
                checked={levelStatusFilter.includes(s)}
                onChange={() => toggleStatus(s)}
                style={{ accentColor: '#26a69a' }}
              />
              {STATUS_LABELS[s]}
            </label>
          ))}
        </div>
      )}

      {/* Signal outcome filter */}
      {layers.signals && (
        <div style={sep}>
          <div style={{ color: '#787b86', fontSize: '0.7rem', marginBottom: 4, fontWeight: 600 }}>
            نتیجه سیگنال
          </div>
          {SIGNAL_OUTCOMES.map(o => (
            <label key={o} style={{
              display: 'flex', alignItems: 'center', gap: 6,
              cursor: 'pointer',
              color: signalOutcomeFilter.includes(o) ? '#d1d4dc' : '#555',
              fontSize: '0.78rem',
            }}>
              <input
                type="checkbox"
                checked={signalOutcomeFilter.includes(o)}
                onChange={() => toggleOutcome(o)}
                style={{ accentColor: '#ef5350' }}
              />
              {OUTCOME_LABELS[o]}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
