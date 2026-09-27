import { useStore, TF } from '../state/store'

const TFS: TF[] = ['M1', 'M5', 'M15', 'H1']

export function TimeframeSwitch() {
  const tf = useStore(s => s.tf)
  const setTf = useStore(s => s.setTf)

  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {TFS.map(t => (
        <button
          key={t}
          onClick={() => setTf(t)}
          style={{
            padding: '3px 10px',
            border: '1px solid',
            borderColor: tf === t ? '#2962ff' : '#2a2e39',
            borderRadius: 4,
            background: tf === t ? '#2962ff22' : 'transparent',
            color: tf === t ? '#2962ff' : '#787b86',
            cursor: 'pointer',
            fontSize: '0.8rem',
            fontWeight: tf === t ? 600 : 400,
          }}
        >
          {t}
        </button>
      ))}
    </div>
  )
}
