import { AXIS_MAX_UA, AXIS_MIN_UA, TIERS } from "../core/constants";

/** Fixed horizontal geometry, in CSS pixels. */
export const GUTTER = 92;
export const PAD_RIGHT = 10;

/** Vertical pieces that do not stretch. */
export const H_RANGE_STRIP = 10;
export const H_LANE = 15;
export const H_AXIS = 22;
export const GAP_Y = 6;

/**
 * Height of the band below the log axis holding everything at or under
 * 100 nA — including zero and the negative readings a correctly calibrated
 * shunt produces. Those values have no place on a log scale and must not be
 * clipped away, so they get a band of their own.
 */
export const SUB_BAND = 20;

/** Below this the decades stop being separable, so the plot never shrinks past it. */
export const MIN_PLOT = 150;

export type YScale = "log" | "lin";

/**
 * Where each part of the chart sits for a given canvas height.
 *
 * The plot is the only piece that stretches: the range strip, the eight digital
 * lanes and the time axis are legible at one size and nothing is gained by
 * growing them. So the chart takes whatever height the viewport leaves and
 * spends all of it on the trace.
 */
export interface ChartLayout {
  /** Plot area height, from the top of the canvas, including the sub-band. */
  plot: number;
  /** y of the range strip. */
  rangeY: number;
  /** y of the first digital lane; null when the lanes are hidden. */
  digitalY: number | null;
  /** y of the time-axis rule. */
  axisY: number;
  /** Canvas height. */
  total: number;
}

/** Vertical space the non-stretching parts need. */
export function fixedHeight(showDigital: boolean): number {
  return GAP_Y + H_RANGE_STRIP + (showDigital ? GAP_Y + H_LANE * 8 : 0) + H_AXIS;
}

export function chartLayout(available: number, showDigital: boolean): ChartLayout {
  const fixed = fixedHeight(showDigital);
  const plot = Math.max(MIN_PLOT, Math.round(available - fixed));
  const rangeY = plot + GAP_Y;
  const digitalY = showDigital ? rangeY + H_RANGE_STRIP + GAP_Y : null;
  const axisY =
    digitalY === null ? rangeY + H_RANGE_STRIP + 4 : digitalY + H_LANE * 8 + 4;
  return { plot, rangeY, digitalY, axisY, total: plot + fixed };
}

/**
 * The current range the plot is showing, in microamps.
 *
 * `full` is the instrument's own span — 100 nA to 1 A — which is what makes two
 * charts from different moments directly comparable. Fitting to the data trades
 * that away for resolution when a DUT uses only a few decades of it, so the
 * console always says which of the two is on screen.
 */
export interface CurrentRange {
  lo: number;
  hi: number;
}

export const FULL_RANGE: CurrentRange = { lo: AXIS_MIN_UA, hi: AXIS_MAX_UA };

/** The sub-band exists only when the axis actually reaches its floor. */
export function hasSubBand(range: CurrentRange): boolean {
  return range.lo <= AXIS_MIN_UA;
}

/**
 * Map a current in microamps to a y coordinate inside the plot.
 *
 * The comparison against the axis floor is `>=`, not `>`: a value exactly at
 * the floor belongs to the axis's bottom tick, not to the sub-band. With `>` the
 * 100 nA gridline label was drawn inside the band and collided with its label.
 *
 * Values outside the range are not clamped. A reading above a zoomed axis runs
 * off the canvas, which is the truth; pinning it to the edge would draw a
 * measurement at a value it never had.
 */
export function yFor(
  uA: number,
  scale: YScale,
  plot: number,
  range: CurrentRange = FULL_RANGE,
): number {
  const band = hasSubBand(range);
  const bottom = band ? plot - SUB_BAND : plot - 2;
  if (scale === "lin") {
    // On a linear axis a floor of 100 nA is zero for every practical purpose,
    // and labelling it otherwise would be precision the scale cannot show.
    const lo = band ? 0 : range.lo;
    const k = (uA - lo) / (range.hi - lo);
    return plot - 2 - k * (plot - 8);
  }
  if (!(uA >= range.lo)) return band ? plot - SUB_BAND / 2 : bottom;
  const k =
    (Math.log10(uA) - Math.log10(range.lo)) / (Math.log10(range.hi) - Math.log10(range.lo));
  return bottom - k * (bottom - 6);
}

/** Widest decade span that still contains the data, so nothing sits on an edge. */
export function fitRange(minUA: number, maxUA: number): CurrentRange {
  if (!Number.isFinite(minUA) || !Number.isFinite(maxUA)) return FULL_RANGE;
  const lo = Math.max(AXIS_MIN_UA, Math.pow(10, Math.floor(Math.log10(Math.max(minUA, AXIS_MIN_UA)))));
  const hi = Math.min(AXIS_MAX_UA, Math.pow(10, Math.ceil(Math.log10(Math.max(maxUA, lo * 10)))));
  return hi > lo ? { lo, hi } : FULL_RANGE;
}

/** Zoom the current axis about `pivot` microamps, staying inside the device span. */
export function zoomRange(range: CurrentRange, factor: number, pivot: number): CurrentRange {
  const floor = Math.log10(AXIS_MIN_UA);
  const ceil = Math.log10(AXIS_MAX_UA);
  const lLo = Math.log10(range.lo);
  const lHi = Math.log10(range.hi);
  const lPivot = Math.min(lHi, Math.max(lLo, Math.log10(Math.max(pivot, AXIS_MIN_UA))));
  // Never below one decade: past that it stops being a log axis.
  const span = Math.min(ceil - floor, Math.max(1, (lHi - lLo) * factor));
  const share = (lPivot - lLo) / Math.max(1e-9, lHi - lLo);
  let lo = lPivot - span * share;
  let hi = lo + span;
  if (lo < floor) {
    lo = floor;
    hi = lo + span;
  }
  if (hi > ceil) {
    hi = ceil;
    lo = hi - span;
  }
  return { lo: Math.pow(10, lo), hi: Math.pow(10, hi) };
}

/** Invert `yFor` on the log scale, so a wheel can zoom about the cursor. */
export function currentAtY(y: number, plot: number, range: CurrentRange): number {
  const bottom = hasSubBand(range) ? plot - SUB_BAND : plot - 2;
  const k = (bottom - y) / Math.max(1, bottom - 6);
  return Math.pow(10, Math.log10(range.lo) + k * (Math.log10(range.hi) - Math.log10(range.lo)));
}

/**
 * Pick the finest tier whose bucket count for `spanSeconds` stays within about
 * three buckets per pixel. Beyond that the extra resolution cannot be drawn and
 * only costs scan time.
 */
export function chooseTier(spanSeconds: number, plotWidth: number, manual: number): number {
  if (manual >= 0) return manual;
  for (let i = 0; i < TIERS.length; i++) {
    if ((spanSeconds * 1000) / TIERS[i]!.ms <= plotWidth * 3.2) return i;
  }
  return TIERS.length - 1;
}

/** Nice-looking time-axis step for a window, in seconds. */
export function axisStep(spanSeconds: number): number {
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if (spanSeconds / s <= 10) return s;
  return steps[steps.length - 1]!;
}

/**
 * Decades to label, coarsened when they would collide.
 *
 * A label needs roughly 15px of vertical room; below that we drop to every
 * second decade rather than letting them overlap.
 */
export function decadeStep(plot: number, range: CurrentRange = FULL_RANGE): number {
  const decades = Math.max(1, Math.log10(range.hi) - Math.log10(range.lo));
  return (plot - SUB_BAND) / decades >= 15 ? 1 : 2;
}

/** Finest useful time window: 200 samples at the finest tier. */
export const MIN_SPAN_S = 0.002;

/** Zoom the time window about `pivot` seconds, keeping that instant under the cursor. */
export function zoomSpan(
  span: number,
  end: number,
  factor: number,
  pivot: number,
  maxSpan: number,
): { span: number; end: number } {
  const next = Math.min(maxSpan, Math.max(MIN_SPAN_S, span * factor));
  // The fraction of the window to the right of the cursor is preserved, so the
  // instant under the pointer does not move while the window grows or shrinks.
  const rightShare = (end - pivot) / Math.max(1e-9, span);
  return { span: next, end: pivot + next * rightShare };
}
