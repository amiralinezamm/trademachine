/**
 * Candlestick patterns layer — circle markers on candleSeries.
 * Uses candleSeries.setMarkers() so markers always align with visible bars.
 */
import { useEffect } from 'react'
import type { IChartApi, ISeriesApi, SeriesMarker, Time } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import type { TF } from '../../state/store'

interface PatternRow {
  ts: number
  pattern: string
  dir: number | null
}

export function usePatternsLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
) {
  useEffect(() => {
    if (!candleSeries) return
    if (!enabled) {
      candleSeries.setMarkers([])
      return
    }
  }, [candleSeries, enabled])

  useEffect(() => {
    if (!enabled || !candleSeries || !visRange) return
    let cancelled = false
    const { from, to } = visRange

    apiGet<{ hits: PatternRow[] }>(`/admin/api/chart/patterns?tf=${_tf}&from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled || !candleSeries) return
        const markers: SeriesMarker<Time>[] = data.hits.map((p) => ({
          time: p.ts as Time,
          position: (p.dir ?? 0) > 0 ? 'belowBar' : 'aboveBar',
          color: (p.dir ?? 0) > 0 ? '#26a69a' : '#ef5350',
          shape: 'circle',
          text: p.pattern,
        }))
        // Sort markers by time (LWC requirement)
        markers.sort((a, b) => (a.time as number) - (b.time as number))
        candleSeries.setMarkers(markers)
      }).catch(() => {})

    return () => {
      cancelled = true
      // Clear markers when range changes or disabled
      if (candleSeries) candleSeries.setMarkers([])
    }
  }, [enabled, visRange?.from, visRange?.to, _tf, candleSeries])
}
