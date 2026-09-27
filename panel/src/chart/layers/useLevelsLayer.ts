/**
 * Levels layer — draws support/resistance zones using ZonePrimitive.
 * α = 0.06 + 0.14 × min(strength / strength_p90, 1)
 * Border: active solid, broken dashed, flipped solid (new color) + ⇄, expired none + α×0.4
 */
import { useEffect, useRef } from 'react'
import type { IChartApi, ISeriesApi, MouseEventParams, Time } from 'lightweight-charts'
import { apiGet } from '../../api/client'
import { ZonePrimitive, type ZoneData } from '../primitives/ZonePrimitive'
import type { TF } from '../../state/store'

interface Level {
  id: number
  kind: 'support' | 'resistance'
  lo: number
  hi: number
  created: number
  ended: number | null
  status: string
  strength: number
  touches: number
  breaks: number
  double_touch: boolean
}

function p90(arr: number[]): number {
  if (!arr.length) return 1
  const sorted = [...arr].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length * 0.9)] || sorted[sorted.length - 1] || 1
}

function levelToZone(l: Level, sp90: number, rightEdge: number | null): ZoneData {
  const alpha = 0.06 + 0.14 * Math.min(l.strength / sp90, 1)
  const isSupport = l.kind === 'support'
  const baseR = isSupport ? '38,166,154' : '239,83,80'

  let fillAlpha = alpha
  let fillColor: string
  let borderColor: string | undefined
  let borderWidth: number | undefined
  let borderStyle: 'solid' | 'dashed' | 'none' = 'none'
  let label: string | undefined

  if (l.status === 'expired') {
    fillAlpha = alpha * 0.4
    fillColor = `rgba(${baseR},${fillAlpha.toFixed(3)})`
    borderStyle = 'none'
  } else if (l.status === 'active') {
    fillColor = `rgba(${baseR},${fillAlpha.toFixed(3)})`
    borderColor = isSupport ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)'
    borderWidth = 1
    borderStyle = 'solid'
  } else if (l.status === 'broken') {
    fillColor = `rgba(${baseR},${fillAlpha.toFixed(3)})`
    borderColor = isSupport ? 'rgba(38,166,154,0.4)' : 'rgba(239,83,80,0.4)'
    borderWidth = 1
    borderStyle = 'dashed'
  } else if (l.status === 'flipped') {
    // Flipped: new role color (opposite of original kind)
    const newR = isSupport ? '239,83,80' : '38,166,154'
    fillColor = `rgba(${newR},${fillAlpha.toFixed(3)})`
    borderColor = isSupport ? 'rgba(239,83,80,0.6)' : 'rgba(38,166,154,0.6)'
    borderWidth = 1
    borderStyle = 'solid'
    label = '⇄'
  } else {
    fillColor = `rgba(${baseR},${fillAlpha.toFixed(3)})`
  }

  return {
    timeFrom: l.created,
    timeTo: l.ended ?? null,
    priceLo: l.lo,
    priceHi: l.hi,
    fillColor,
    borderColor,
    borderWidth,
    borderStyle,
    label,
  }
}

let _popover: HTMLDivElement | null = null

function showPopover(level: Level, x: number, y: number) {
  if (_popover) _popover.remove()
  const d = document.createElement('div')
  d.style.cssText = `position:fixed;left:${x}px;top:${y}px;background:#1e222d;border:1px solid #2a2e39;
    border-radius:4px;padding:8px 12px;color:#d1d4dc;font-size:12px;z-index:9999;min-width:160px;pointer-events:none`
  d.innerHTML = `
    <div style="font-weight:700;margin-bottom:4px">Level #${level.id}</div>
    <div>نوع: ${level.kind === 'support' ? 'حمایت' : 'مقاومت'}</div>
    <div>وضعیت: ${level.status}</div>
    <div>قدرت: ${level.strength.toFixed(3)}</div>
    <div>برخورد: ${level.touches}</div>
    <div>شکست: ${level.breaks}</div>
    ${level.double_touch ? '<div style="color:#26a69a">✓ double touch</div>' : ''}
  `
  document.body.appendChild(d)
  _popover = d
}

function hidePopover() { _popover?.remove(); _popover = null }

export function useLevelsLayer(
  chart: IChartApi | null,
  candleSeries: ISeriesApi<'Candlestick'> | null,
  visRange: { from: number; to: number } | null,
  _tf: TF,
  enabled: boolean,
  statusFilter: string[],
) {
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const primRef = useRef<ZonePrimitive | null>(null)
  const levelsRef = useRef<Level[]>([])

  // Create / destroy own series
  useEffect(() => {
    if (!chart || !candleSeries) return
    if (!enabled) {
      if (seriesRef.current) {
        try { chart.removeSeries(seriesRef.current) } catch {}
        seriesRef.current = null
        primRef.current = null
      }
      hidePopover()
      return
    }

    const s = chart.addLineSeries({
      color: 'transparent',
      lineWidth: 1,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    })
    seriesRef.current = s
    const prim = new ZonePrimitive(chart, candleSeries, 'bottom')
    s.attachPrimitive(prim)
    primRef.current = prim

    return () => {
      if (seriesRef.current && chart) {
        try { chart.removeSeries(seriesRef.current) } catch {}
      }
      seriesRef.current = null
      primRef.current = null
      hidePopover()
    }
  }, [chart, candleSeries, enabled])

  // Fetch levels when range or filter changes
  useEffect(() => {
    if (!enabled || !primRef.current || !visRange) return
    let cancelled = false
    const { from, to } = visRange

    apiGet<{ levels: Level[] }>(
      `/admin/api/chart/levels?from=${from}&to=${to}&status=${statusFilter.join(',')}`
    ).then((data) => {
      if (cancelled || !primRef.current) return
      levelsRef.current = data.levels
      const sp90 = p90(data.levels.map((l) => l.strength))
      const filtered = data.levels.filter((l) => statusFilter.includes(l.status))
      primRef.current.setZones(filtered.map((l) => levelToZone(l, sp90, null)))
    }).catch(() => {})

    return () => { cancelled = true }
  }, [enabled, visRange?.from, visRange?.to, statusFilter.join(',')])

  // Click handler for popover
  useEffect(() => {
    if (!chart || !enabled) return
    const handler = (param: MouseEventParams<Time>) => {
      if (!param.point || !param.sourceEvent) { hidePopover(); return }
      const prim = primRef.current
      if (!prim || !candleSeries) { hidePopover(); return }

      const price = candleSeries.coordinateToPrice(param.point.y)
      const time = chart.timeScale().coordinateToTime(param.point.x)
      if (price === null || time === null) { hidePopover(); return }

      const ts = time as number
      const hit = levelsRef.current.find(
        (l) =>
          price >= l.lo && price <= l.hi &&
          l.created <= ts && (l.ended === null || l.ended >= ts)
      )
      if (hit) {
        showPopover(hit, param.sourceEvent.clientX + 8, param.sourceEvent.clientY + 8)
      } else {
        hidePopover()
      }
    }

    chart.subscribeCrosshairMove(handler)
    return () => { chart.unsubscribeCrosshairMove(handler); hidePopover() }
  }, [chart, candleSeries, enabled])
}
