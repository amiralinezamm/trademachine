/**
 * VBandPrimitive — draws vertical time-band background overlays.
 * Used for news-window highlighting.
 */
import type {
  ISeriesPrimitive,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  IChartApi,
  Time,
} from 'lightweight-charts'

export interface VBandData {
  timeFrom: number   // epoch seconds UTC
  timeTo: number
  fillColor: string
  label?: string
}

class VBandRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(
    private readonly _chart: IChartApi,
    private readonly _bands: VBandData[],
  ) {}

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  draw(target: any): void {
    target.useBitmapCoordinateSpace((scope: any) => {
      const ctx = scope.context
      const hr = scope.horizontalPixelRatio
      const vr = scope.verticalPixelRatio
      const h = scope.bitmapSize.height

      for (const b of this._bands) {
        const x1css = this._chart.timeScale().timeToCoordinate(b.timeFrom as Time)
        const x2css = this._chart.timeScale().timeToCoordinate(b.timeTo as Time)
        if (x1css === null || x2css === null) continue

        const bx1 = Math.round(x1css * hr)
        const bx2 = Math.round(x2css * hr)
        const bw = bx2 - bx1
        if (bw <= 0) continue

        ctx.fillStyle = b.fillColor
        ctx.fillRect(bx1, 0, bw, h)

        if (b.label) {
          ctx.save()
          ctx.fillStyle = 'rgba(200,200,200,0.8)'
          ctx.font = `${Math.round(10 * vr)}px sans-serif`
          ctx.fillText(b.label, bx1 + 2 * hr, Math.round(14 * vr))
          ctx.restore()
        }
      }
    })
  }
}

class VBandPaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _source: VBandPrimitive) {}

  renderer(): VBandRenderer {
    return new VBandRenderer(this._source._chart, this._source._bands)
  }

  zOrder(): 'bottom' | 'normal' | 'top' { return 'bottom' }
}

export class VBandPrimitive implements ISeriesPrimitive<Time> {
  _bands: VBandData[] = []
  private _view: VBandPaneView
  private _requestUpdate?: () => void

  constructor(readonly _chart: IChartApi) {
    this._view = new VBandPaneView(this)
  }

  attached({ requestUpdate }: { requestUpdate: () => void }) {
    this._requestUpdate = requestUpdate
  }
  detached() { this._requestUpdate = undefined }

  setBands(bands: VBandData[]) {
    this._bands = bands
    this._requestUpdate?.()
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] { return [this._view] }
}
