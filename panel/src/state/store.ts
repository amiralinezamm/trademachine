import { create } from 'zustand'

export type TF = 'M1' | 'M5' | 'M15' | 'H1'

export interface LayerState {
  candles: boolean
  levels: boolean
  signals: boolean
  gaps: boolean
  rounds: boolean
  fib: boolean
  news: boolean
  patterns: boolean
  regime: boolean
}

const DEFAULT_LAYERS: LayerState = {
  candles: true,
  levels: true,
  signals: false,
  gaps: false,
  rounds: false,
  fib: false,
  news: false,
  patterns: false,
  regime: false,
}

const LS_LAYERS_KEY = 'xauusd_layers'
const LS_LEVEL_STATUS_KEY = 'xauusd_level_status'
const LS_SIGNAL_OUTCOME_KEY = 'xauusd_signal_outcome'
const LS_TF_KEY = 'xauusd_tf'
const LS_TEHRAN_KEY = 'xauusd_tehran'

function loadLayers(): LayerState {
  try {
    const raw = localStorage.getItem(LS_LAYERS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<LayerState>
      return { ...DEFAULT_LAYERS, ...parsed }
    }
  } catch {}
  return DEFAULT_LAYERS
}

function loadStringArray(key: string, fallback: string[]): string[] {
  try {
    const raw = localStorage.getItem(key)
    if (raw) return JSON.parse(raw)
  } catch {}
  return fallback
}

function saveLayers(layers: LayerState) {
  try { localStorage.setItem(LS_LAYERS_KEY, JSON.stringify(layers)) } catch {}
}
function saveStringArray(key: string, arr: string[]) {
  try { localStorage.setItem(key, JSON.stringify(arr)) } catch {}
}

interface AppState {
  tf: TF
  layers: LayerState
  isTehran: boolean
  asOf: null
  visRange: { from: number; to: number } | null
  levelStatusFilter: string[]
  signalOutcomeFilter: string[]

  setTf: (tf: TF) => void
  setLayer: (name: keyof LayerState, on: boolean) => void
  toggleTimezone: () => void
  setVisRange: (range: { from: number; to: number } | null) => void
  setLevelStatusFilter: (statuses: string[]) => void
  setSignalOutcomeFilter: (outcomes: string[]) => void
}

const initialTf: TF = (() => {
  try { return (localStorage.getItem(LS_TF_KEY) as TF) || 'M5' } catch { return 'M5' }
})()

const initialTehran: boolean = (() => {
  try { return localStorage.getItem(LS_TEHRAN_KEY) === 'true' } catch { return false }
})()

export const useStore = create<AppState>((set) => ({
  tf: initialTf,
  layers: loadLayers(),
  isTehran: initialTehran,
  asOf: null,
  visRange: null,
  levelStatusFilter: loadStringArray(LS_LEVEL_STATUS_KEY, ['active', 'broken', 'flipped']),
  signalOutcomeFilter: loadStringArray(LS_SIGNAL_OUTCOME_KEY, ['tp', 'sl', 'timeout', 'open', 'level_invalidated']),

  setTf: (tf) => {
    try { localStorage.setItem(LS_TF_KEY, tf) } catch {}
    set({ tf })
  },
  setLayer: (name, on) => set((s) => {
    const layers = { ...s.layers, [name]: on }
    saveLayers(layers)
    return { layers }
  }),
  toggleTimezone: () => set((s) => {
    const next = !s.isTehran
    try { localStorage.setItem(LS_TEHRAN_KEY, String(next)) } catch {}
    return { isTehran: next }
  }),
  setVisRange: (range) => set({ visRange: range }),
  setLevelStatusFilter: (statuses) => {
    saveStringArray(LS_LEVEL_STATUS_KEY, statuses)
    set({ levelStatusFilter: statuses })
  },
  setSignalOutcomeFilter: (outcomes) => {
    saveStringArray(LS_SIGNAL_OUTCOME_KEY, outcomes)
    set({ signalOutcomeFilter: outcomes })
  },
}))
