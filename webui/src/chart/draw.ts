import { QUANTILE_MIN_UA, RANGE_COUNT, TIERS } from "../core/constants";
import { fmtCurrent, fmtRelTime } from "../core/format";
import type { ColumnAggregate } from "./columns";
import {
  FULL_RANGE,
  GUTTER,
  H_LANE,
  H_RANGE_STRIP,
  PAD_RIGHT,
  SUB_BAND,
  axisStep,
  decadeStep,
  hasSubBand,
  yFor,
  type ChartLayout,
  type CurrentRange,
  type YScale,
} from "./geometry";

/** Theme colours, read from CSS custom properties once per theme change. */
export interface Palette {
  ink2: string;
  ink3: string;
  grid: string;
  gridStrong: string;
  trace: string;
  traceSoft: string;
  rule: string;
  panel: string;
  panel2: string;
  panel3: string;
  loss: string;
  lossSoft: string;
  hatch: string;
  warn: string;
  ranges: string[];
}

export function readPalette(el: HTMLElement): Palette {
  const cs = getComputedStyle(el);
  const v = (name: string) => cs.getPropertyValue(name).trim();
  const ranges: string[] = [];
  for (let r = 0; r < RANGE_COUNT; r++) ranges.push(v(`--r${r}`));
  return {
    ink2: v("--ink-2"),
    ink3: v("--ink-3"),
    grid: v("--grid"),
    gridStrong: v("--grid-strong"),
    trace: v("--trace"),
    traceSoft: v("--trace-soft"),
    rule: v("--rule"),
    panel: v("--panel"),
    panel2: v("--panel-2"),
    panel3: v("--panel-3"),
    loss: v("--loss"),
    lossSoft: v("--loss-soft"),
    hatch: v("--hatch"),
    warn: v("--warn"),
    ranges,
  };
}

export interface ChartLabels {
  /** Row label for the measurement-range strip. */
  range: string;
  /** Caption for the 200 nA distribution floor line. */
  floor: string;
  /** Shown over the stretch of window this tier does not retain. */
  noHistory: string;
}

export interface DrawOptions {
  ctx: CanvasRenderingContext2D;
  cols: ColumnAggregate;
  layout: ChartLayout;
  scale: YScale;
  /** Current axis window. Full device span unless the reader zoomed or fitted. */
  range: CurrentRange;
  plotWidth: number;
  now: number;
  labels: ChartLabels;
  channelRoles: readonly string[];
  palette: Palette;
}

const MONO = '10px "IBM Plex Mono", ui-monospace, monospace';
const MONO_SMALL = '9px "IBM Plex Mono", ui-monospace, monospace';

export function drawChart(o: DrawOptions): void {
  const { ctx, cols, layout, plotWidth: w } = o;
  const span = cols.t1 - cols.t0;

  ctx.clearRect(0, 0, GUTTER + w + PAD_RIGHT, layout.total);
  ctx.font = MONO;
  ctx.textBaseline = "middle";

  drawAxis(o);
  drawMissingHistory(o, span);
  drawGaps(o);
  drawBand(o);
  drawMeanLine(o);
  drawRangeStrip(o);
  if (layout.digitalY !== null) drawDigital(o, layout.digitalY);
  drawTimeAxis(o, span);
}

/**
 * Decade rules. These are not decoration: they are the instrument's own
 * structure — five shunts spanning 200 nA to 1 A, seven decades of it.
 */
function drawAxis(o: DrawOptions): void {
  const { ctx, palette: p, plotWidth: w, layout, range } = o;
  const plot = layout.plot;
  ctx.textAlign = "right";

  if (o.scale === "log") {
    const step = decadeStep(plot, range);
    const first = Math.round(Math.log10(range.lo));
    const last = Math.round(Math.log10(range.hi));
    for (let e = first; e <= last; e++) {
      const value = Math.pow(10, e);
      const y = Math.round(yFor(value, "log", plot, range)) + 0.5;
      const major = e === 0 || e === 3 || e === 6;
      ctx.strokeStyle = major ? p.gridStrong : p.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(GUTTER, y);
      ctx.lineTo(GUTTER + w, y);
      ctx.stroke();
      // Every rule is drawn; only the labels thin out when the plot is short.
      if (step === 1 || major || e === first) {
        ctx.fillStyle = p.ink3;
        ctx.fillText(fmtCurrent(value, 0), GUTTER - 8, y);
      }
    }

    // The 200 nA distribution floor is a property of the instrument, so it is
    // drawn where a reader can see which quantiles are bounded by it — but only
    // when the axis actually covers it.
    const floorVisible = QUANTILE_MIN_UA >= range.lo && QUANTILE_MIN_UA <= range.hi;
    const yFloor = Math.round(yFor(QUANTILE_MIN_UA, "log", plot, range)) + 0.5;
    if (floorVisible) {
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.globalAlpha = 0.75;
    ctx.strokeStyle = p.warn;
    ctx.beginPath();
    ctx.moveTo(GUTTER, yFloor);
    ctx.lineTo(GUTTER + w, yFloor);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = p.warn;
    ctx.textAlign = "left";
    ctx.fillText(o.labels.floor, GUTTER + 6, yFloor - 8);
    }

    // Zero and negative readings only have a home when the axis reaches its
    // floor; a zoomed-in axis simply does not contain them.
    if (hasSubBand(range)) {
      const ySub = plot - SUB_BAND;
      ctx.strokeStyle = p.rule;
      ctx.beginPath();
      ctx.moveTo(GUTTER, ySub + 0.5);
      ctx.lineTo(GUTTER + w, ySub + 0.5);
      ctx.stroke();
      ctx.fillStyle = p.ink3;
      ctx.textAlign = "right";
      ctx.fillText("≤0", GUTTER - 8, plot - 6);
    }
  } else {
    for (let k = 0; k <= 5; k++) {
      const value = range.lo + (k / 5) * (range.hi - range.lo);
      const y = Math.round(yFor(value, "lin", plot, range)) + 0.5;
      ctx.strokeStyle = k === 0 ? p.gridStrong : p.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(GUTTER, y);
      ctx.lineTo(GUTTER + w, y);
      ctx.stroke();
      ctx.fillStyle = p.ink3;
      ctx.fillText(fmtCurrent(value, k === 0 ? 0 : 1), GUTTER - 8, y);
    }
  }
}

/** Say where the retained history stops rather than drawing an empty plot. */
function drawMissingHistory(o: DrawOptions, span: number): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  if (!(cols.oldest > cols.t0) || !Number.isFinite(cols.oldest)) return;
  const xEnd = GUTTER + Math.max(0, ((cols.oldest - cols.t0) / span) * w);
  if (xEnd <= GUTTER + 1) return;
  ctx.fillStyle = p.panel3;
  ctx.globalAlpha = 0.65;
  ctx.fillRect(GUTTER, 0, xEnd - GUTTER, layout.plot);
  ctx.globalAlpha = 1;
  ctx.fillStyle = p.ink3;
  ctx.textAlign = "left";
  ctx.fillText(o.labels.noHistory, GUTTER + 8, 16);
}

/** Lost samples are hatched, never interpolated across. */
function drawGaps(o: DrawOptions): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  const plot = layout.plot;
  ctx.fillStyle = p.lossSoft;
  for (let x = 0; x < w; x++) if (cols.gap[x]! > 0) ctx.fillRect(GUTTER + x, 0, 1, plot);

  ctx.save();
  ctx.beginPath();
  ctx.rect(GUTTER, 0, w, plot);
  ctx.clip();
  ctx.strokeStyle = p.hatch;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = 0; x < w; x++) {
    if (cols.gap[x]! > 0 && (GUTTER + x) % 4 === 0) {
      ctx.moveTo(GUTTER + x, plot);
      ctx.lineTo(GUTTER + x + plot, 0);
    }
  }
  ctx.stroke();
  ctx.restore();
}

/** The min–max band. A bare mean would hide the spikes worth measuring. */
function drawBand(o: DrawOptions): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  ctx.fillStyle = p.traceSoft;
  for (let x = 0; x < w; x++) {
    if (cols.n[x] === 0) continue;
    const y1 = yFor(cols.max[x]!, o.scale, layout.plot, o.range);
    const y2 = yFor(cols.min[x]!, o.scale, layout.plot, o.range);
    ctx.fillRect(GUTTER + x, y1, 1, Math.max(1, y2 - y1));
  }
}

function drawMeanLine(o: DrawOptions): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  ctx.strokeStyle = p.trace;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  let pen = false;
  for (let x = 0; x < w; x++) {
    // The line breaks at a gap. Joining across one would draw a measurement
    // that was never taken.
    if (cols.n[x] === 0 || cols.gap[x]! > 0) {
      pen = false;
      continue;
    }
    const y = yFor(cols.sum[x]! / cols.n[x]!, o.scale, layout.plot, o.range);
    if (!pen) {
      ctx.moveTo(GUTTER + x + 0.5, y);
      pen = true;
    } else {
      ctx.lineTo(GUTTER + x + 0.5, y);
    }
  }
  ctx.stroke();
}

/** Which shunt was in circuit. A column holding several is split, not averaged. */
function drawRangeStrip(o: DrawOptions): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  const y = layout.rangeY;
  ctx.fillStyle = p.panel2;
  ctx.fillRect(GUTTER, y, w, H_RANGE_STRIP);
  for (let x = 0; x < w; x++) {
    const mask = cols.rangeMask[x]!;
    if (!mask) continue;
    const set: number[] = [];
    for (let r = 0; r < RANGE_COUNT; r++) if (mask & (1 << r)) set.push(r);
    const h = H_RANGE_STRIP / set.length;
    for (let i = 0; i < set.length; i++) {
      ctx.fillStyle = p.ranges[set[i]!]!;
      ctx.fillRect(GUTTER + x, y + i * h, 1, h);
    }
  }
  ctx.fillStyle = p.ink3;
  ctx.textAlign = "right";
  ctx.fillText(o.labels.range, GUTTER - 8, y + H_RANGE_STRIP / 2);
}

/**
 * D0-D7 on the shared timeline. A column whose bucket saw both levels is drawn
 * as a solid block: after decimation "steady high" and "toggled" are different
 * facts, and a single level would assert the wrong one.
 */
function drawDigital(o: DrawOptions, top0: number): void {
  const { ctx, cols, palette: p, plotWidth: w } = o;

  for (let ch = 0; ch < 8; ch++) {
    const top = top0 + ch * H_LANE;
    const hi = top + 3;
    const lo = top + H_LANE - 5;

    ctx.strokeStyle = p.rule;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(GUTTER, lo + 0.5);
    ctx.lineTo(GUTTER + w, lo + 0.5);
    ctx.stroke();

    ctx.textAlign = "right";
    ctx.fillStyle = p.ink2;
    ctx.fillText(`D${ch}`, GUTTER - 58, top + H_LANE / 2);
    ctx.fillStyle = p.ink3;
    ctx.font = MONO_SMALL;
    ctx.fillText(o.channelRoles[ch] ?? "", GUTTER - 8, top + H_LANE / 2);
    ctx.font = MONO;

    ctx.strokeStyle = p.trace;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let pen = false;
    for (let x = 0; x < w; x++) {
      if (cols.n[x] === 0) {
        pen = false;
        continue;
      }
      const any = (cols.logicAny[x]! >> ch) & 1;
      const all = (cols.logicAll[x]! >> ch) & 1;
      if (any !== all) {
        ctx.stroke();
        ctx.beginPath();
        pen = false;
        ctx.fillStyle = p.traceSoft;
        ctx.fillRect(GUTTER + x, hi, 1, lo - hi);
        continue;
      }
      const y = any ? hi + 0.5 : lo + 0.5;
      if (!pen) {
        ctx.moveTo(GUTTER + x + 0.5, y);
        pen = true;
      } else {
        ctx.lineTo(GUTTER + x + 0.5, y);
      }
    }
    ctx.stroke();
  }
}

function drawTimeAxis(o: DrawOptions, span: number): void {
  const { ctx, cols, palette: p, plotWidth: w, layout } = o;
  const y = layout.axisY;
  ctx.strokeStyle = p.rule;
  ctx.beginPath();
  ctx.moveTo(GUTTER, y + 0.5);
  ctx.lineTo(GUTTER + w, y + 0.5);
  ctx.stroke();

  const step = axisStep(span);
  ctx.fillStyle = p.ink3;
  ctx.textAlign = "center";
  const first = Math.ceil(cols.t0 / step) * step;
  for (let t = first; t <= cols.t1 + 1e-9; t += step) {
    const x = GUTTER + ((t - cols.t0) / span) * w;
    ctx.strokeStyle = p.rule;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, y);
    ctx.lineTo(x + 0.5, y + 4);
    ctx.stroke();
    ctx.fillText(fmtRelTime(t - o.now), x, y + 14);
  }
}

/** Crosshair on the overlay canvas, so a hover never repaints the trace. */
export function drawCrosshair(
  ctx: CanvasRenderingContext2D,
  cols: ColumnAggregate,
  x: number | null,
  scale: YScale,
  layout: ChartLayout,
  palette: Palette,
  plotWidth: number,
  range: CurrentRange = FULL_RANGE,
): void {
  ctx.clearRect(0, 0, GUTTER + plotWidth + PAD_RIGHT, layout.total);
  if (x === null) return;

  ctx.strokeStyle = palette.ink3;
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.moveTo(GUTTER + x + 0.5, 0);
  ctx.lineTo(GUTTER + x + 0.5, layout.axisY);
  ctx.stroke();
  ctx.setLineDash([]);

  if (cols.n[x]! > 0) {
    const y = yFor(cols.sum[x]! / cols.n[x]!, scale, layout.plot, range);
    ctx.fillStyle = palette.panel;
    ctx.strokeStyle = palette.trace;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(GUTTER + x + 0.5, y, 3.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}

export function tierLabel(tier: number): string {
  return TIERS[tier]!.label;
}
