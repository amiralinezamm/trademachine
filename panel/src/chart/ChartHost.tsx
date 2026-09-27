import { useEffect, useRef, useCallback, useState } from 'react'
import {
  createChart,
  ColorType,
  CrosshairMode,
  TickMarkType,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  UTCTimestamp,
} from 'lightweight-charts'
import { useStore, TF } from '../state/store'
import { apiGet } from '../api/client'
import { useLevelsLayer } from './layers/useLevelsLayer'
import { useSignalsLayer } from './layers/useSignalsLayer'
import { useGapsLayer } from './layers/useGapsLayer'
import { useRoundsLayer } from './layers/useRoundsLayer'
import { useFibLayer } from './layers/useFibLayer'
import { useNewsLayer } from './layers/useNewsLayer'
import { usePatternsLayer } from './layers/usePatternsLayer'
import { useRegimeLayer } from './layers/useRegimeLayer'

type Candle = { t: number; o: number; h: number; l: number; c: number }

const TF_CHUNK_S: Record<TF, number> = {
  M1: 3 * 86400,
  M5: 30 * 86400,
  M15: 60 * 86400,
  H1: 180 * 86400,
}
const TF_MAX_S: Record<TF, number> = {
  M1: 7 * 86400,
  M5: 90 * 86400,
  M15: 180 * 86400,
  H1: 730 * 86400,
}
const TF_SECONDS: Record<TF, number> = {
  M1: 60,
  M5: 300,
  M15: 900,
  H1: 3600,
}

const _cache = new Map<string, Candle[]>()

async function fetchCandles(tf: TF, from: number, to: number): Promise<Candle[]> {
  const max = TF_MAX_S[tf]
  const safeFrom = Math.max(from, to - max)
  if (safeFrom >= to) return []
  const key = `${tf}|${safeFrom}|${to}`
  if (_cache.has(key)) return _cache.get(key)!
  const data = await apiGet<{ candles: Candle[] }>(
    `/admin/api/chart/candles?tf=${tf}&from=${safeFrom}&to=${to}`
  )
  _cache.set(key, data.candles)
  return data.candles
}

function toLw(c: Candle): CandlestickData<UTCTimestamp> {
  return { time: c.t as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c }
}

function makeTehranFormatter(isTehran: boolean) {
  return (time: number) => {
    const d = new Date(time * 1000)
    if (isTehran) {
      return new Intl.DateTimeFormat('en', {
        timeZone: 'Asia/Tehran',
        month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(d).replace(',', '')
    }
    return d.toISOString().slice(5, 16).replace('T', ' ')
  }
}

function makeTehranTickFormatter(isTehran: boolean) {
  const tz = isTehran ? 'Asia/Tehran' : 'UTC'
  return (time: UTCTimestamp, tickMarkType: TickMarkType, _locale: string): string => {
    const d = new Date((time as number) * 1000)
    switch (tickMarkType) {
      case TickMarkType.Year:
        return new Intl.DateTimeFormat('en', { timeZone: tz, year: 'numeric' }).format(d)
      case TickMarkType.Month:
        return new Intl.DateTimeFormat('en', { timeZone: tz, month: 'short', year: '2-digit' }).format(d)
      case TickMarkType.DayOfMonth:
        return new Intl.DateTimeFormat('en', { timeZone: tz, month: 'short', day: 'numeric' }).format(d)
      case TickMarkType.Time:
        return new Intl.DateTimeFormat('en', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false }).format(d)
      case TickMarkType.TimeWithSeconds:
        return new Intl.DateTimeFormat('en', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(d)
      default:
        return new Intl.DateTimeFormat('en', { timeZone: tz, month: 'short', day: 'numeric' }).format(d)
    }
  }
}

function CandleCountdown({ tf }: { tf: TF }) {
  const [text, setText] = useState('')

  useEffect(() => {
    const intervalS = TF_SECONDS[tf]
    function update() {
      const now = Math.floor(Date.now() / 1000)
      const remaining = intervalS - (now % intervalS)
      const mm = Math.floor(remaining / 60).toString().padStart(2, '0')
      const ss = (remaining % 60).toString().padStart(2, '0')
      setText(`${mm}:${ss}`)
    }
    update()
    const id = setInterval(update, 1000)
    return () => clearInterval(id)
  }, [tf])

  if (!text) return null
  return (
    <div style={{
      position: 'absolute',
      bottom: 28,
      right: 70,
      color: '#787b86',
      fontSize: '11px',
      fontFamily: 'monospace',
      pointerEvents: 'none',
      zIndex: 10,
      userSelect: 'none',
    }}>
      {text}
    </div>
  )
}

export function ChartHost() {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const candlesRef = useRef<Candle[]>([])
  const loadedFromRef = useRef(0)
  const loadedToRef = useRef(0)
  const fetchingRef = useRef(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const [chart, setChart] = useState<IChartApi | null>(null)
  const [candleSeries, setCandleSeries] = useState<ISeriesApi<'Candlestick'> | null>(null)

  const tf = useStore(s => s.tf)
  const isTehran = useStore(s => s.isTehran)
  const layers = useStore(s => s.layers)
  const setVisRangeStore = useStore(s => s.setVisRange)
  const visRange = useStore(s => s.visRange)
  const levelStatusFilter = useStore(s => s.levelStatusFilter)
  const signalOutcomeFilter = useStore(s => s.signalOutcomeFilter)

  const tfRef = useRef(tf)
  const isTehranRef = useRef(isTehran)
  useEffect(() => { tfRef.current = tf }, [tf])
  useEffect(() => { isTehranRef.current = isTehran }, [isTehran])

  // Apply timezone when isTehran changes (or when chart becomes available)
  useEffect(() => {
    if (!chart) return
    chart.applyOptions({
      localization: { timeFormatter: makeTehranFormatter(isTehran) },
      timeScale: { tickMarkFormatter: makeTehranTickFormatter(isTehran) },
    })
  }, [isTehran, chart])

  const setData = useCallback((candles: Candle[], restoreRange?: { from: UTCTimestamp; to: UTCTimestamp } | null) => {
    if (!seriesRef.current || !chartRef.current) return
    seriesRef.current.setData(candles.map(toLw))
    if (restoreRange) {
      chartRef.current.timeScale().setVisibleRange(restoreRange)
    }
  }, [])

  const loadInitial = useCallback(async (currentTf: TF) => {
    const now = Math.floor(Date.now() / 1000)
    const chunk = TF_CHUNK_S[currentTf]
    const from = now - chunk
    const candles = await fetchCandles(currentTf, from, now)
    if (tfRef.current !== currentTf) return
    candlesRef.current = candles
    loadedFromRef.current = from
    loadedToRef.current = now
    setData(candles)
    chartRef.current?.timeScale().fitContent()
  }, [setData])

  const handleRangeChange = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(async () => {
      if (!chartRef.current) return

      const visibleRange = chartRef.current.timeScale().getVisibleRange()
      if (visibleRange) {
        setVisRangeStore({ from: visibleRange.from as number, to: visibleRange.to as number })
      }

      if (fetchingRef.current) return
      if (!visibleRange) return
      const visFrom = visibleRange.from as number
      const loadedSpan = loadedToRef.current - loadedFromRef.current
      const threshold = loadedSpan * 0.15

      if (visFrom <= loadedFromRef.current + threshold) {
        fetchingRef.current = true
        try {
          const currentTf = tfRef.current
          const newTo = loadedFromRef.current
          const newFrom = newTo - TF_CHUNK_S[currentTf]
          const newCandles = await fetchCandles(currentTf, newFrom, newTo)
          if (!newCandles.length || tfRef.current !== currentTf) return
          const savedRange = chartRef.current?.timeScale().getVisibleRange() as
            | { from: UTCTimestamp; to: UTCTimestamp }
            | null
          candlesRef.current = [...newCandles, ...candlesRef.current]
          loadedFromRef.current = newFrom
          setData(candlesRef.current, savedRange)
        } finally {
          fetchingRef.current = false
        }
      }
    }, 250)
  }, [setData, setVisRangeStore])

  // Initialize chart once
  useEffect(() => {
    if (!containerRef.current) return
    const c = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#131722' },
        textColor: '#d1d4dc',
      },
      grid: {
        vertLines: { color: '#1e222d' },
        horzLines: { color: '#1e222d' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#1e222d' },
      timeScale: {
        borderColor: '#1e222d',
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: makeTehranTickFormatter(isTehranRef.current),
      },
      width: containerRef.current.offsetWidth,
      height: containerRef.current.offsetHeight,
      // Use current isTehran value (from ref) at chart creation time
      localization: { timeFormatter: makeTehranFormatter(isTehranRef.current) },
    })
    chartRef.current = c

    const series = c.addCandlestickSeries({
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    })
    seriesRef.current = series

    c.timeScale().subscribeVisibleLogicalRangeChange(handleRangeChange)

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        c.applyOptions({
          width: containerRef.current.offsetWidth,
          height: containerRef.current.offsetHeight,
        })
      }
    })
    ro.observe(containerRef.current)

    setChart(c)
    setCandleSeries(series)

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      c.timeScale().unsubscribeVisibleLogicalRangeChange(handleRangeChange)
      ro.disconnect()
      c.remove()
      chartRef.current = null
      seriesRef.current = null
      setChart(null)
      setCandleSeries(null)
    }
  }, [handleRangeChange])

  // Reload candles when TF changes
  useEffect(() => {
    candlesRef.current = []
    loadedFromRef.current = 0
    loadedToRef.current = 0
    loadInitial(tf)
  }, [tf, loadInitial])

  // Auto-refresh: fetch latest candles every 60s to show the last few bars
  useEffect(() => {
    const id = setInterval(async () => {
      if (!seriesRef.current || !chartRef.current || fetchingRef.current) return
      const now = Math.floor(Date.now() / 1000)
      const from = loadedToRef.current - TF_SECONDS[tfRef.current] * 10  // last 10 bars
      try {
        const newCandles = await apiGet<{ candles: Candle[] }>(
          `/admin/api/chart/candles?tf=${tfRef.current}&from=${from}&to=${now}`
        )
        if (!newCandles.candles.length) return
        const existing = candlesRef.current
        const newTs = new Set(newCandles.candles.map((c) => c.t))
        const merged = [
          ...existing.filter((c) => !newTs.has(c.t)),
          ...newCandles.candles,
        ].sort((a, b) => a.t - b.t)
        candlesRef.current = merged
        loadedToRef.current = now
        const savedRange = chartRef.current.timeScale().getVisibleRange() as
          | { from: UTCTimestamp; to: UTCTimestamp }
          | null
        setData(merged, savedRange)
      } catch {}
    }, 60_000)
    return () => clearInterval(id)
  }, [setData])

  useLevelsLayer(chart, candleSeries, visRange, tf, layers.levels, levelStatusFilter)
  useSignalsLayer(chart, candleSeries, visRange, tf, layers.signals, signalOutcomeFilter)
  useGapsLayer(chart, candleSeries, visRange, tf, layers.gaps)
  useRoundsLayer(chart, candleSeries, visRange, tf, layers.rounds)
  useFibLayer(chart, candleSeries, visRange, tf, layers.fib)
  useNewsLayer(chart, candleSeries, visRange, tf, layers.news)
  usePatternsLayer(chart, candleSeries, visRange, tf, layers.patterns)
  useRegimeLayer(chart, candleSeries, visRange, tf, layers.regime)

  return (
    <div style={{ flex: 1, minWidth: 0, height: '100%', position: 'relative' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      <CandleCountdown tf={tf} />
    </div>
  )
}
