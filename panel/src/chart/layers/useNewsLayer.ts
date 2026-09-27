/**
 * News layer — vertical time bands with full event detail.
 * impact 1 (High) → red, impact 2 (Medium) → amber.
 * Title shows impact, currency, and up to 2 event names.
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import { VBandPrimitive, type VBandData } from '../primitives/VBandPrimitive'
import type { TF } from '../../state/store'

interface NewsWindow {
  start: number
  end: number
  impact: number
  titles: string[]
  ccy: string
  doubled: boolean
}

const IMPACT_COLOR: Record<number, string> = {
  1: 'rgba(239,83,80,0.15)',
  2: 'rgba(255,183,77,0.10)',
}

const IMPACT_LABEL: Record<number, string> = {
  1: 'H',
  2: 'M',
}

export function useNewsLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
) {
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const primRef = useRef<VBandPrimitive | null>(null)

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
    const prim = new VBandPrimitive(chart)
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

    apiGet<{ windows: NewsWindow[] }>(`/admin/api/chart/news-windows?from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled || !primRef.current) return
        const bands: VBandData[] = data.windows.map((w) => {
          const impactStr = IMPACT_LABEL[w.impact] ?? '?'
          const titleParts = w.titles.slice(0, 2).join(' / ')
          const doubled = w.doubled ? ' [x2]' : ''
          const title = `[${impactStr}][${w.ccy}] ${titleParts}${doubled}`
          return {
            timeFrom: w.start,
            timeTo: w.end,
            fillColor: IMPACT_COLOR[w.impact] ?? 'rgba(120,123,134,0.06)',
            title,
          }
        })
        primRef.current.setBands(bands)
      }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to])
}
