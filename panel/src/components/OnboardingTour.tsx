import { useEffect, useState } from 'react'

const STORAGE_KEY = 'xauusd_tour_done'

interface Step {
  title: string
  body: string
  anchor?: string  // CSS selector for highlight (optional)
}

const STEPS: Step[] = [
  { title: 'نمودار XAUUSD', body: 'نمودار اصلی قیمت طلا. برای زوم از اسکرول موس استفاده کنید.' },
  { title: 'پنل لایه‌ها', body: 'از پنل سمت راست می‌توانید لایه‌های مختلف را روشن/خاموش کنید: سطوح، سیگنال‌ها، گپ‌ها، فیبوناچی و...' },
  { title: 'تایم‌فریم', body: 'از بالای صفحه تایم‌فریم را تغییر دهید: M1، M5، M15، H1.' },
  { title: 'ساعت UTC / تهران', body: 'دکمه UTC/تهران در هدر: زمان‌های نمودار را بین UTC و وقت تهران تغییر می‌دهد.' },
  { title: 'وضعیت اتصال', body: 'نقطه رنگی در هدر وضعیت اتصال EA را نشان می‌دهد. سبز + موج = متصل. قرمز = قطع.' },
  { title: 'صفحات پنل', body: 'از تب‌های بالا بین نمودار، داشبورد (سیگنال‌ها و PnL) و لاگ‌ها (قطعی‌ها و سرور) جابجا شوید.' },
]

export function OnboardingTour() {
  const [visible, setVisible] = useState(false)
  const [step, setStep] = useState(0)

  useEffect(() => {
    try {
      if (!localStorage.getItem(STORAGE_KEY)) {
        setVisible(true)
      }
    } catch {}
  }, [])

  function close() {
    try { localStorage.setItem(STORAGE_KEY, '1') } catch {}
    setVisible(false)
  }

  function next() {
    if (step < STEPS.length - 1) setStep(s => s + 1)
    else close()
  }

  function prev() {
    if (step > 0) setStep(s => s - 1)
  }

  if (!visible) {
    // Show a subtle "نمایش مجدد راهنما" button so user can re-open
    return (
      <button
        onClick={() => { setStep(0); setVisible(true) }}
        title="راهنما"
        style={{
          background: 'transparent', border: 'none', color: '#787b86',
          cursor: 'pointer', fontSize: '0.75rem', padding: '3px 6px',
        }}
      >
        ?
      </button>
    )
  }

  const cur = STEPS[step]

  return (
    <div style={s.overlay}>
      <div style={s.box}>
        <div style={s.progress}>
          {STEPS.map((_, i) => (
            <div key={i} style={{ ...s.dot, background: i === step ? '#2962ff' : '#2a2e39' }} />
          ))}
        </div>
        <div style={s.title}>{cur.title}</div>
        <div style={s.body}>{cur.body}</div>
        <div style={s.actions}>
          {step > 0 && (
            <button style={s.btnSecondary} onClick={prev}>قبلی</button>
          )}
          <button style={s.btnSkip} onClick={close}>رد کردن</button>
          <button style={s.btnPrimary} onClick={next}>
            {step < STEPS.length - 1 ? 'بعدی' : 'متوجه شدم'}
          </button>
        </div>
        <div style={{ fontSize: '0.7rem', color: '#787b86', textAlign: 'center', marginTop: 8 }}>
          {step + 1} / {STEPS.length}
        </div>
      </div>
    </div>
  )
}

const s: Record<string, React.CSSProperties> = {
  overlay: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    zIndex: 9999, direction: 'rtl',
  },
  box: {
    background: '#1e222d', border: '1px solid #2a2e39', borderRadius: 10,
    padding: '24px 28px', width: 360, maxWidth: '90vw',
    boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
  },
  progress: { display: 'flex', gap: 6, marginBottom: 16 },
  dot: { width: 8, height: 8, borderRadius: '50%', transition: 'background 0.2s' },
  title: { fontSize: '1.1rem', fontWeight: 700, color: '#d1d4dc', marginBottom: 10 },
  body: { fontSize: '0.85rem', color: '#9598a1', lineHeight: 1.6, marginBottom: 20 },
  actions: { display: 'flex', gap: 8, justifyContent: 'flex-end' },
  btnPrimary: {
    background: '#2962ff', border: 'none', borderRadius: 4, color: '#fff',
    padding: '6px 16px', cursor: 'pointer', fontSize: '0.85rem',
  },
  btnSecondary: {
    background: 'transparent', border: '1px solid #2a2e39', borderRadius: 4, color: '#d1d4dc',
    padding: '6px 12px', cursor: 'pointer', fontSize: '0.85rem',
  },
  btnSkip: {
    background: 'transparent', border: 'none', color: '#787b86',
    padding: '6px 8px', cursor: 'pointer', fontSize: '0.8rem', marginRight: 'auto',
  },
}
