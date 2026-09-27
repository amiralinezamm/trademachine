/**
 * Regime layer — 8px bar at the bottom of the chart pane.
 * Colors: trend rgba(38,166,154,0.35), range rgba(41,98,255,0.35), other grey.
 * NOTE: regime table is empty in current DB — this draws nothing until populated.
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import { RegimePrimitive, type RegimeSegment } from '../primitives/RegimePrimitive'
import type { TF } from '../../state/store'

interface RegimeRow {
  from: number
  to: number
  regime: string
}

export function useRegimeLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
) {
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const primRef = useRef<RegimePrimitive | null>(null)

  useEffect(() => {
    if (!chart || !candleSeries) return
    if (!enabled) {
      if (seriesRef.current) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
      primRef.current = null
      return
    }
    const s = chart.addLineSeries({
      color: 'transparent', lineWidth: 1,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    })
    seriesRef.current = s
    const prim = new RegimePrimitive(chart)
    s.attachPrimitive(prim)
    primRef.current = prim

    return () => {
      if (seriesRef.current && chart) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
      primRef.current = null
    }
  }, [chart, candleSeries, enabled])

  useEffect(() => {
    if (!enabled || !primRef.current || !visRange) return
    let cancelled = false
    const { from, to } = visRange

    apiGet<{ regimes: RegimeRow[] }>(`/admin/api/chart/regime?from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled || !primRef.current) return
        const segs: RegimeSegment[] = data.regimes.map((r) => ({
          from: r.from,
          to: r.to,
          regime: r.regime,
        }))
        primRef.current.setSegments(segs)
      }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to])
}
