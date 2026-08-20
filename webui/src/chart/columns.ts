import { RANGE_COUNT, TIERS } from "../core/constants";
import type { Decimator } from "../core/decimator";

/**
 * Per-pixel-column aggregation of the bucket ring.
 *
 * One column can cover several buckets, so the aggregation happens here rather
 * than in the draw pass — and it keeps min, max, sum and count, never a bare
 * mean. Buffers are allocated once per width and refilled, because this runs
 * thirty times a second.
 */
export class ColumnAggregate {
  width: number;
  min: Float32Array;
  max: Float32Array;
  sum: Float64Array;
  n: Uint32Array;
  gap: Uint32Array;
  rangeMask: Uint8Array;
  logicAny: Uint8Array;
  logicAll: Uint8Array;
  edges: Uint32Array;

  /** Timeline start of the oldest bucket the chosen tier still retains. */
  oldest = Infinity;
  /** Window bounds actually drawn, session-relative seconds. */
  t0 = 0;
  t1 = 1;
  tier = 0;

  constructor(width: number) {
    this.width = width;
    this.min = new Float32Array(width);
    this.max = new Float32Array(width);
    this.sum = new Float64Array(width);
    this.n = new Uint32Array(width);
    this.gap = new Uint32Array(width);
    this.rangeMask = new Uint8Array(width);
    this.logicAny = new Uint8Array(width);
    this.logicAll = new Uint8Array(width);
    this.edges = new Uint32Array(width);
  }

  private reset(): void {
    this.min.fill(Infinity);
    this.max.fill(-Infinity);
    this.sum.fill(0);
    this.n.fill(0);
    this.gap.fill(0);
    this.rangeMask.fill(0);
    this.logicAny.fill(0);
    this.logicAll.fill(0xff);
    this.edges.fill(0);
  }

  mean(x: number): number | null {
    return this.n[x] ? this.sum[x]! / this.n[x]! : null;
  }

  ranges(x: number): number[] {
    const out: number[] = [];
    for (let r = 0; r < RANGE_COUNT; r++) if (this.rangeMask[x]! & (1 << r)) out.push(r);
    return out;
  }

  /**
   * Fill the columns for `[now - span, now]` from tier `tier`.
   *
   * Scans the ring newest-first and stops as soon as a bucket ends before the
   * window starts, so the cost follows the window rather than the ring.
   */
  collect(decimator: Decimator, tier: number, now: number, span: number): void {
    this.reset();
    this.tier = tier;
    this.t1 = now;
    this.t0 = now - span;

    const ring = decimator.rings[tier]!;
    this.oldest = ring.oldest();
    const bucketSeconds = TIERS[tier]!.ms / 1000;
    const w = this.width;

    for (let k = ring.count - 1; k >= 0; k--) {
      const i = ring.idx(k);
      const t = ring.t0[i]!;
      if (t > this.t1) continue;
      if (t + bucketSeconds < this.t0) break;

      let col = Math.floor(((t - this.t0) / span) * w);
      if (col < 0) col = 0;
      if (col >= w) col = w - 1;

      if (ring.n[i]! > 0) {
        if (ring.min[i]! < this.min[col]!) this.min[col] = ring.min[i]!;
        if (ring.max[i]! > this.max[col]!) this.max[col] = ring.max[i]!;
        this.sum[col] += ring.sum[i]!;
        this.n[col] += ring.n[i]!;
        this.logicAny[col] |= ring.logicAny[i]!;
        this.logicAll[col] &= ring.logicAll[i]!;
      }
      this.gap[col] += ring.gap[i]!;
      this.rangeMask[col] |= ring.rangeMask[i]!;
      this.edges[col] += ring.edges[i]!;
    }
  }
}

/** Aggregate statistics over whatever the current window actually holds. */
export interface WindowStats {
  mean: number;
  min: number;
  max: number;
  /** Samples present. */
  n: number;
  /** Samples known missing. */
  gap: number;
  spanSeconds: number;
  /** Present samples over expected samples for the window. */
  covered: number;
  /** Columns in which each range appeared — not a share of samples. */
  rangeColumns: number[];
  columnsWithData: number;
}

export function windowStats(cols: ColumnAggregate, sampleRateHz: number): WindowStats | null {
  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  let n = 0;
  let gap = 0;
  let columnsWithData = 0;
  const rangeColumns = new Array<number>(RANGE_COUNT).fill(0);

  for (let x = 0; x < cols.width; x++) {
    if (cols.n[x]! > 0) {
      if (cols.min[x]! < min) min = cols.min[x]!;
      if (cols.max[x]! > max) max = cols.max[x]!;
      sum += cols.sum[x]!;
      n += cols.n[x]!;
      columnsWithData++;
      for (let r = 0; r < RANGE_COUNT; r++) if (cols.rangeMask[x]! & (1 << r)) rangeColumns[r]!++;
    }
    gap += cols.gap[x]!;
  }
  if (n === 0) return null;

  const spanSeconds = cols.t1 - cols.t0;
  return {
    mean: sum / n,
    min,
    max,
    n,
    gap,
    spanSeconds,
    covered: n / Math.max(1, Math.round(spanSeconds * sampleRateHz)),
    rangeColumns,
    columnsWithData,
  };
}
