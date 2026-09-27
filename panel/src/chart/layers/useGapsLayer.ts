/**
 * Gaps layer — rgba(255,183,77,α), α = 0.05 + 0.15 × weight
 * price range: low..high, time: open_ts..filled_ts (or right edge)
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import { ZonePrimitive, type ZoneData } from '../primitives/ZonePrimitive'
import type { TF } from '../../state/store'

interface Gap {
  id: number
  open_ts: number
  low: number
  high: number
  weight: number
  filled: boolean
  filled_ts: number | null
}

export function useGapsLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
) {
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const primRef = useRef<ZonePrimitive | null>(null)

  useEffect(() => {
    if (!chart || !candleSeries) return
    if (!enabled) {
      if (seriesRef.current) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
      primRef.current = null
      return
    }
    const s = chart.addLineSeries({ color: 'transparent', lineWidth: 1, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false })
    seriesRef.current = s
    const prim = new ZonePrimitive(chart, candleSeries, 'bottom')
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
    apiGet<{ gaps: Gap[] }>(`/admin/api/chart/gaps?from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled || !primRef.current) return
        const zones: ZoneData[] = data.gaps.map((g) => {
          // Minimum 0.12 so even weight~0 gaps are clearly visible; scale up with weight
          const alpha = Math.min(0.12 + 0.25 * (g.weight ?? 0), 0.45)
          return {
            timeFrom: g.open_ts,
            timeTo: g.filled_ts ?? null,
            priceLo: g.low,
            priceHi: g.high,
            fillColor: `rgba(255,183,77,${alpha.toFixed(3)})`,
            borderColor: 'rgba(255,183,77,0.4)',
            borderWidth: 1,
            borderStyle: g.filled ? 'dashed' : 'solid',
          } satisfies ZoneData
        })
        primRef.current.setZones(zones)
      }).catch(() => {})
    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to])
}
