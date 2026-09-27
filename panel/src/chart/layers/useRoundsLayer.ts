/**
 * Round numbers layer — dashed horizontal price lines.
 * Thickness: 100→2, 50→1.5, 25→1, 10→1, 5→0.5
 * Fetches /admin/api/chart/rounds using visible price range from the chart.
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi, IPriceLine, LineStyle } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import type { TF } from '../../state/store'

interface RoundLevel {
  price: number
  multiple: number
  weight: number
}

const MULTIPLE_WIDTH: Record<number, number> = {
  100: 2,
  50: 1,
  25: 1,
  10: 1,
  5: 1,
}

// LineStyle.Dashed = 1
const DASHED_STYLE = 1 as unknown as LineStyle

export function useRoundsLayer(
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
    const s = chart.addLineSeries({ color: 'transparent', lineWidth: 1, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false })
    seriesRef.current = s
    return () => {
      priceLines.current = []
      if (seriesRef.current && chart) { try { chart.removeSeries(seriesRef.current) } catch {} seriesRef.current = null }
    }
  }, [chart, candleSeries, enabled])

  useEffect(() => {
    if (!enabled || !seriesRef.current || !candleSeries || !chart || !visRange) return
    let cancelled = false

    // Sample two y-coordinates to get the visible price range
    // The chart container DOM element is the first child of the chart's parent div
    const chartContainer = (chart as unknown as Record<string, unknown>)['_private__container'] as HTMLElement | undefined
    const heightPx = chartContainer?.offsetHeight ?? 600
    const priceTop = candleSeries.coordinateToPrice(10)
    const priceBot = candleSeries.coordinateToPrice(heightPx - 10)

    let price_min: number, price_max: number
    if (priceTop !== null && priceBot !== null) {
      const lo = Math.min(priceTop, priceBot)
      const hi = Math.max(priceTop, priceBot)
      const span = Math.min(hi - lo + 100, 1800)
      const mid = (lo + hi) / 2
      price_min = Math.max(0, mid - span / 2)
      price_max = mid + span / 2
    } else {
      // Fallback for XAUUSD
      price_min = 3000
      price_max = 4800
    }

    apiGet<{ levels: RoundLevel[] }>(`/admin/api/chart/rounds?price_min=${price_min.toFixed(2)}&price_max=${price_max.toFixed(2)}`)
      .then((data) => {
        if (cancelled || !seriesRef.current) return

        // Remove old price lines
        priceLines.current.forEach((pl) => { try { seriesRef.current?.removePriceLine(pl) } catch {} })
        priceLines.current = []

        const s = seriesRef.current
        for (const lvl of data.levels) {
          const width = MULTIPLE_WIDTH[lvl.multiple] ?? 1
          try {
            const pl = s.createPriceLine({
              price: lvl.price,
              color: 'rgba(120,123,134,0.5)',
              lineWidth: width as 1 | 2 | 3 | 4,
              lineStyle: DASHED_STYLE,
              axisLabelVisible: true,
              title: String(lvl.price),
            })
            priceLines.current.push(pl)
          } catch {}
        }
      }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to, chart, candleSeries])
}
