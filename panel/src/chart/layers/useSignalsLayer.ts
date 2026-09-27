/**
 * Signals layer — redesigned for clarity.
 * Open signals: prominent entry price line + SL/TP zones + arrow marker.
 * Closed signals: arrow marker only (no zones), dimmer color.
 * Limited to 15 most recent signals in visible range to avoid clutter.
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi, SeriesMarker, Time, IPriceLine, MouseEventParams } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import { ZonePrimitive, type ZoneData } from '../primitives/ZonePrimitive'
import type { TF } from '../../state/store'

export interface Signal {
  id: number
  ts: number
  dir: string
  entry: number
  sl: number
  tp: number
  conf: number | null
  outcome: string | null
  pnl: number | null
  rule_version: string
  components: unknown
}

const OUTCOME_ARROW: Record<string, string> = {
  tp: '#26a69a',
  sl: '#ef5350',
  timeout: '#787b86',
  open: '#2962ff',
  level_invalidated: '#f57c00',
}

function outcomeColor(outcome: string | null): string {
  return OUTCOME_ARROW[outcome ?? 'open'] ?? '#2962ff'
}

const DASHED = 1 as unknown as import('lightweight-charts').LineStyle

export function useSignalsLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
  outcomeFilter: string[],
) {
  const markerSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const zoneSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const primRef = useRef<ZonePrimitive | null>(null)
  const entryLinesRef = useRef<IPriceLine[]>([])
  const entrySeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const signalsRef = useRef<Signal[]>([])

  useEffect(() => {
    if (!chart || !candleSeries) return
    if (!enabled) {
      if (markerSeriesRef.current) { try { chart.removeSeries(markerSeriesRef.current) } catch {} markerSeriesRef.current = null }
      if (zoneSeriesRef.current) { try { chart.removeSeries(zoneSeriesRef.current) } catch {} zoneSeriesRef.current = null }
      if (entrySeriesRef.current) { try { chart.removeSeries(entrySeriesRef.current) } catch {} entrySeriesRef.current = null }
      primRef.current = null
      entryLinesRef.current = []
      return
    }

    const ms = chart.addLineSeries({
      color: 'transparent', lineWidth: 1,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    })
    markerSeriesRef.current = ms

    const zs = chart.addLineSeries({
      color: 'transparent', lineWidth: 1,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    })
    const prim = new ZonePrimitive(chart, candleSeries, 'normal')
    zs.attachPrimitive(prim)
    zoneSeriesRef.current = zs
    primRef.current = prim

    // Entry price lines live on this series
    const es = chart.addLineSeries({
      color: 'transparent', lineWidth: 1,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    })
    entrySeriesRef.current = es

    return () => {
      if (markerSeriesRef.current && chart) { try { chart.removeSeries(markerSeriesRef.current) } catch {} markerSeriesRef.current = null }
      if (zoneSeriesRef.current && chart) { try { chart.removeSeries(zoneSeriesRef.current) } catch {} zoneSeriesRef.current = null }
      if (entrySeriesRef.current && chart) { try { chart.removeSeries(entrySeriesRef.current) } catch {} entrySeriesRef.current = null }
      primRef.current = null
      entryLinesRef.current = []
    }
  }, [chart, candleSeries, enabled])

  useEffect(() => {
    if (!enabled || !markerSeriesRef.current || !visRange) return
    let cancelled = false
    const { from, to } = visRange

    apiGet<{ signals: Signal[] }>(`/admin/api/chart/signals?from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled) return
        let sigs = data.signals.filter((s) => outcomeFilter.includes(s.outcome ?? 'open'))
        // Limit to 15 most recent to prevent clutter
        sigs = sigs.slice(-15)
        signalsRef.current = sigs

        const ms = markerSeriesRef.current
        const es = entrySeriesRef.current

        // Arrow markers
        if (ms) {
          const markers: SeriesMarker<Time>[] = sigs.map((sig) => ({
            time: sig.ts as Time,
            position: sig.dir === 'buy' ? 'belowBar' : 'aboveBar',
            color: outcomeColor(sig.outcome),
            shape: sig.dir === 'buy' ? 'arrowUp' : 'arrowDown',
            text: sig.conf != null ? `${Math.round(sig.conf * 100)}%` : '',
            size: sig.outcome == null ? 2 : 1,  // open signals get larger arrow
          }))
          ms.setMarkers(markers)
        }

        // Entry price lines (open signals only)
        if (es) {
          entryLinesRef.current.forEach((pl) => { try { es.removePriceLine(pl) } catch {} })
          entryLinesRef.current = []
          const openSigs = sigs.filter((s) => s.outcome == null && s.entry)
          for (const sig of openSigs) {
            try {
              const pl = es.createPriceLine({
                price: sig.entry,
                color: sig.dir === 'buy' ? '#26a69a' : '#ef5350',
                lineWidth: 2,
                lineStyle: DASHED,
                axisLabelVisible: true,
                title: `${sig.dir.toUpperCase()} entry`,
              })
              entryLinesRef.current.push(pl)
            } catch {}
          }
        }

        // SL/TP zones: only for open signals
        if (primRef.current) {
          const zones: ZoneData[] = []
          for (const sig of sigs.filter((s) => s.outcome == null && s.entry && s.sl && s.tp)) {
            const tpLo = Math.min(sig.entry, sig.tp)
            const tpHi = Math.max(sig.entry, sig.tp)
            const slLo = Math.min(sig.entry, sig.sl)
            const slHi = Math.max(sig.entry, sig.sl)
            zones.push({ timeFrom: sig.ts, timeTo: null, priceLo: tpLo, priceHi: tpHi, fillColor: 'rgba(38,166,154,0.12)' })
            zones.push({ timeFrom: sig.ts, timeTo: null, priceLo: slLo, priceHi: slHi, fillColor: 'rgba(239,83,80,0.12)' })
          }
          primRef.current.setZones(zones)
        }
      }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to, outcomeFilter.join(',')])

  useEffect(() => {
    if (!chart || !enabled) return
    const handler = (param: MouseEventParams<Time>) => {
      if (!param.point || !param.sourceEvent || !candleSeries) return
      const time = chart.timeScale().coordinateToTime(param.point.x)
      if (time === null) return
      const ts = time as number
      const TOLERANCE = 120
      const hit = signalsRef.current.find((s) => Math.abs(s.ts - ts) <= TOLERANCE)
      if (hit) {
        window.dispatchEvent(new CustomEvent('panel:signal-click', { detail: hit }))
      }
    }
    chart.subscribeClick(handler)
    return () => chart.unsubscribeClick(handler)
  }, [chart, candleSeries, enabled])
}
