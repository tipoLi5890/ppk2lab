import { QUANTILE_MIN_UA } from "./constants";

/**
 * Log-spaced distribution grid, mirroring `capture/stats.py`.
 *
 * Quantiles come from a histogram rather than from stored samples: the same
 * accumulator has to run live during an 8-hour capture, where keeping the
 * samples would mean 2.9 billion of them. The grid is log-spaced because the
 * instrument is — its five shunts span 200 nA to 1 A, and both its resolution
 * and its accuracy are proportional statements.
 *
 * Bin 0 is the underflow bin: zero, negatives, and anything at or below the
 * 200 nA floor land there. A quantile served from it is an **upper bound**, not
 * a measurement, which is what `atFloor` reports.
 */
export const HIST_DECADES = 7;
export const HIST_PER_DECADE = 24;
export const HIST_BINS = HIST_DECADES * HIST_PER_DECADE;

export function newHistogram(): Float64Array {
  return new Float64Array(HIST_BINS + 1);
}

export function histAdd(h: Float64Array, uA: number): void {
  if (!(uA > QUANTILE_MIN_UA)) {
    h[0]++;
    return;
  }
  const decade = Math.log10(uA / QUANTILE_MIN_UA);
  const i = Math.min(HIST_BINS, 1 + Math.floor(decade * HIST_PER_DECADE));
  h[i]++;
}

/** Representative value for a bin: its geometric midpoint. */
export function histBinValue(i: number): number {
  if (i === 0) return QUANTILE_MIN_UA;
  return QUANTILE_MIN_UA * Math.pow(10, (i - 0.5) / HIST_PER_DECADE);
}

export interface QuantileResult {
  value: number | null;
  /** True when the value came from the 200 nA floor rather than from a measurement. */
  atFloor: boolean;
}

export function histQuantile(h: Float64Array, q: number): QuantileResult {
  let total = 0;
  for (let i = 0; i < h.length; i++) total += h[i];
  if (total === 0) return { value: null, atFloor: false };
  const target = q * total;
  let acc = 0;
  for (let i = 0; i < h.length; i++) {
    acc += h[i];
    if (acc >= target) return { value: histBinValue(i), atFloor: i === 0 };
  }
  return { value: histBinValue(h.length - 1), atFloor: false };
}

export function histAddScaled(into: Float64Array, from: Float64Array, times: number): void {
  for (let i = 0; i < into.length; i++) into[i] += from[i] * times;
}
