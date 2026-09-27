/**
 * ZonePrimitive — draws filled price×time rectangles on a LWC v4 chart.
 * Attach to any ISeriesApi with series.attachPrimitive(primitive).
 * Uses the attached chart + a reference candlestick series for coordinate conversion.
 */
import type {
  ISeriesPrimitive,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  IChartApi,
  ISeriesApi,
  Time,
} from 'lightweight-charts'

export type BorderStyle = 'solid' | 'dashed' | 'none'

export interface ZoneData {
  timeFrom: number        // epoch seconds UTC
  timeTo: number | null   // null → draw to right edge
  priceLo: number
  priceHi: number
  fillColor: string
  borderColor?: string
  borderWidth?: number    // CSS pixels
  borderStyle?: BorderStyle
  /** ⇄ label drawn when flipped */
  label?: string
}

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------

class ZoneRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(
    private readonly _chart: IChartApi,
    private readonly _series: ISeriesApi<'Candlestick'>,
    private readonly _zones: ZoneData[],
  ) {}

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  draw(target: any): void {
    target.useBitmapCoordinateSpace((scope: any) => {
      const ctx = scope.context
      const hr = scope.horizontalPixelRatio
      const vr = scope.verticalPixelRatio
      const rightEdge = scope.bitmapSize.width

      for (const z of this._zones) {
        const x1css = this._chart.timeScale().timeToCoordinate(z.timeFrom as Time)
        const y1css = this._series.priceToCoordinate(z.priceHi)
        const y2css = this._series.priceToCoordinate(z.priceLo)

        if (x1css === null || y1css === null || y2css === null) continue

        let x2bmp: number
        if (z.timeTo !== null && z.timeTo !== undefined) {
          const x2css = this._chart.timeScale().timeToCoordinate(z.timeTo as Time)
          x2bmp = x2css !== null ? Math.round(x2css * hr) : rightEdge
        } else {
          x2bmp = rightEdge
        }

        const bx1 = Math.round(x1css * hr)
        const by1 = Math.round(Math.min(y1css, y2css) * vr)
        const bw  = x2bmp - bx1
        const bh  = Math.round(Math.abs(y1css - y2css) * vr)

        if (bw <= 0 || bh <= 0) continue

        ctx.fillStyle = z.fillColor
        ctx.fillRect(bx1, by1, bw, bh)

        const bs = z.borderStyle ?? 'none'
        if (bs !== 'none' && z.borderColor && (z.borderWidth ?? 0) > 0) {
          ctx.save()
          ctx.strokeStyle = z.borderColor
          ctx.lineWidth = (z.borderWidth ?? 1) * Math.min(hr, vr)
          if (bs === 'dashed') {
            ctx.setLineDash([4 * hr, 4 * hr])
          } else {
            ctx.setLineDash([])
          }
          ctx.strokeRect(bx1 + 0.5, by1 + 0.5, bw - 1, bh - 1)
          ctx.restore()
        }

        if (z.label) {
          ctx.save()
          ctx.fillStyle = z.borderColor ?? '#ffffff'
          ctx.font = `bold ${Math.round(11 * vr)}px sans-serif`
          ctx.fillText(z.label, bx1 + 4 * hr, by1 + Math.round(13 * vr))
          ctx.restore()
        }
      }
    })
  }
}

// ---------------------------------------------------------------------------
// PaneView
// ---------------------------------------------------------------------------

class ZonePaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _source: ZonePrimitive) {}

  renderer(): ZoneRenderer {
    return new ZoneRenderer(
      this._source._chart,
      this._source._series,
      this._source._zones,
    )
  }

  zOrder(): 'bottom' | 'normal' | 'top' {
    return this._source._zOrder
  }
}

// ---------------------------------------------------------------------------
// Primitive
// ---------------------------------------------------------------------------

export class ZonePrimitive implements ISeriesPrimitive<Time> {
  _zones: ZoneData[] = []
  _zOrder: 'bottom' | 'normal' | 'top'
  private _view: ZonePaneView
  private _requestUpdate?: () => void

  constructor(
    readonly _chart: IChartApi,
    readonly _series: ISeriesApi<'Candlestick'>,
    zOrder: 'bottom' | 'normal' | 'top' = 'bottom',
  ) {
    this._zOrder = zOrder
    this._view = new ZonePaneView(this)
  }

  attached({ requestUpdate }: { requestUpdate: () => void }) {
    this._requestUpdate = requestUpdate
  }

  detached() {
    this._requestUpdate = undefined
  }

  setZones(zones: ZoneData[]) {
    this._zones = zones
    this._requestUpdate?.()
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return [this._view]
  }
}
