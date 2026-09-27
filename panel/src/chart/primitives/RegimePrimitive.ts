/**
 * RegimePrimitive — 8px regime bar at the bottom of the chart pane.
 */
import type {
  ISeriesPrimitive,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  IChartApi,
  Time,
} from 'lightweight-charts'

export interface RegimeSegment {
  from: number   // epoch seconds
  to: number
  regime: string // 'trend' | 'range' | null/other → grey
}

const REGIME_COLORS: Record<string, string> = {
  trend: 'rgba(38,166,154,0.35)',
  range: 'rgba(41,98,255,0.35)',
}
const REGIME_DEFAULT = 'rgba(120,123,134,0.35)'
const BAR_H_CSS = 8

class RegimeRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(
    private readonly _chart: IChartApi,
    private readonly _segs: RegimeSegment[],
  ) {}

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  draw(target: any): void {
    target.useBitmapCoordinateSpace((scope: any) => {
      const ctx = scope.context
      const hr = scope.horizontalPixelRatio
      const vr = scope.verticalPixelRatio
      const h  = scope.bitmapSize.height
      const barH = Math.round(BAR_H_CSS * vr)
      const y = h - barH

      for (const seg of this._segs) {
        const x1css = this._chart.timeScale().timeToCoordinate(seg.from as Time)
        const x2css = this._chart.timeScale().timeToCoordinate(seg.to as Time)
        if (x1css === null || x2css === null) continue
        const bx1 = Math.round(x1css * hr)
        const bx2 = Math.round(x2css * hr)
        if (bx2 <= bx1) continue
        ctx.fillStyle = REGIME_COLORS[seg.regime] ?? REGIME_DEFAULT
        ctx.fillRect(bx1, y, bx2 - bx1, barH)
      }
    })
  }
}

class RegimePaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _src: RegimePrimitive) {}
  renderer() { return new RegimeRenderer(this._src._chart, this._src._segs) }
  zOrder(): 'bottom' { return 'bottom' }
}

export class RegimePrimitive implements ISeriesPrimitive<Time> {
  _segs: RegimeSegment[] = []
  private _view: RegimePaneView
  private _requestUpdate?: () => void

  constructor(readonly _chart: IChartApi) {
    this._view = new RegimePaneView(this)
  }

  attached({ requestUpdate }: { requestUpdate: () => void }) { this._requestUpdate = requestUpdate }
  detached() { this._requestUpdate = undefined }

  setSegments(segs: RegimeSegment[]) { this._segs = segs; this._requestUpdate?.() }
  paneViews() { return [this._view] as const }
}
