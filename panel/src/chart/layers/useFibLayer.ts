/**
 * Fibonacci layer — horizontal price lines for confluence levels only.
 * confluence_level_id is a boolean from the API (true = overlapping with a key level).
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi, IPriceLine } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import type { TF } from '../../state/store'

interface FibLevel {
  id: number
  price: number
  ratio: number
  confluence_level_id: boolean | null
}

const SOLID_STYLE = 0 as unknown as import('lightweight-charts').LineStyle

export function useFibLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
) {
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const priceLines = useRef<IPriceLine[]>([])

  useEffect(() => {
    if (!chart || !candleSeries) return
    if (!enabled) {
      priceLines.current.forEach((pl) => { try { seriesRef.current?.removePriceLine(pl) } catch {} })
      priceLines.current = []
      if (seriesRef.current) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
      return
    }
    const s = chart.addLineSeries({
      color: 'transparent', lineWidth: 1,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    })
    seriesRef.current = s
    return () => {
      priceLines.current = []
      if (seriesRef.current && chart) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
    }
  }, [chart, candleSeries, enabled])

  useEffect(() => {
    if (!enabled || !seriesRef.current || !visRange) return
    let cancelled = false
    const { from, to } = visRange

    apiGet<{ levels: FibLevel[] }>(`/admin/api/chart/fib?from=${from}&to=${to}`)
      .then((data) => {
        if (cancelled || !seriesRef.current) return

        priceLines.current.forEach((pl) => { try { seriesRef.current?.removePriceLine(pl) } catch {} })
        priceLines.current = []

        const s = seriesRef.current
        // Show all fib levels; highlight confluence ones
        for (const lvl of data.levels) {
          const isConf = lvl.confluence_level_id === true
          try {
            const pl = s.createPriceLine({
              price: lvl.price,
              color: isConf ? 'rgba(255,183,77,0.85)' : 'rgba(255,183,77,0.35)',
              lineWidth: isConf ? 2 : 1,
              lineStyle: SOLID_STYLE,
              axisLabelVisible: true,
              title: `${lvl.ratio}${isConf ? ' *' : ''}`,
            })
            priceLines.current.push(pl)
          } catch {}
        }
      }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to])
}
