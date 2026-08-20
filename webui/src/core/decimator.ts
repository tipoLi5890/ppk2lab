import { SAMPLES_PER_BUCKET, TIERS } from "./constants";
import { histAdd, newHistogram } from "./histogram";

/**
 * Loss-aware multi-resolution decimation of the 100 kS/s stream.
 *
 * The device produces 400 kB/s. Nothing downstream of this module ever sees a
 * raw sample: it sees buckets, and a bucket keeps **min, max, mean and count**
 * rather than a bare mean. That is the rule in `docs/decimation.md` and it is
 * not a detail — a bare mean hides exactly the current spikes this instrument
 * was bought to see.
 *
 * Tiers are hierarchical: a tier-k+1 bucket is ten tier-k buckets folded
 * together, so a sample is aggregated once and the cost of the coarse tiers is
 * O(1) rather than O(tiers). The same shape is intended for the Python
 * supervisor, so the two can be checked against each other.
 *
 * Lost samples are counted, never interpolated over. `gap` on a bucket is the
 * number of samples that should have been there and were not; a consumer that
 * ignores it will silently report a mean over a hole.
 */

export interface Bucket {
  /** Start time of the bucket on the session timeline, in seconds. */
  t0: number;
  /** Minimum current in the bucket, microamps. Meaningless when `n === 0`. */
  min: number;
  max: number;
  /** Sum of currents, microamps. Divide by `n` for the mean. */
  sum: number;
  /** Samples actually present. */
  n: number;
  /** Samples known to be missing. */
  gap: number;
  /** Bit r set when measurement range r appeared in this bucket. */
  rangeMask: number;
  /** OR of the D0-D7 logic bytes seen. */
  logicAny: number;
  /** AND of the D0-D7 logic bytes seen. */
  logicAll: number;
  /** Total bit transitions counted across the bucket. */
  edges: number;
}

function popcount(x: number): number {
  let c = 0;
  while (x) {
    x &= x - 1;
    c++;
  }
  return c;
}

/**
 * Fixed-capacity ring of buckets held in parallel typed arrays.
 *
 * Parallel arrays rather than an array of objects: at 1000 buckets per second
 * per tier, per-bucket object allocation is what a long session would pay for.
 */
export class BucketRing {
  readonly cap: number;
  count = 0;
  private head = 0;

  readonly t0: Float64Array;
  readonly min: Float32Array;
  readonly max: Float32Array;
  readonly sum: Float64Array;
  readonly n: Uint32Array;
  readonly gap: Uint32Array;
  readonly rangeMask: Uint8Array;
  readonly logicAny: Uint8Array;
  readonly logicAll: Uint8Array;
  readonly edges: Uint16Array;

  constructor(cap: number) {
    this.cap = cap;
    this.t0 = new Float64Array(cap);
    this.min = new Float32Array(cap);
    this.max = new Float32Array(cap);
    this.sum = new Float64Array(cap);
    this.n = new Uint32Array(cap);
    this.gap = new Uint32Array(cap);
    this.rangeMask = new Uint8Array(cap);
    this.logicAny = new Uint8Array(cap);
    this.logicAll = new Uint8Array(cap);
    this.edges = new Uint16Array(cap);
  }

  push(b: Bucket): void {
    const i = this.head;
    this.t0[i] = b.t0;
    this.min[i] = b.min;
    this.max[i] = b.max;
    this.sum[i] = b.sum;
    this.n[i] = b.n;
    this.gap[i] = b.gap;
    this.rangeMask[i] = b.rangeMask;
    this.logicAny[i] = b.logicAny;
    this.logicAll[i] = b.logicAll;
    this.edges[i] = b.edges;
    this.head = (i + 1) % this.cap;
    if (this.count < this.cap) this.count++;
  }

  /** Physical slot for logical position `k`, where 0 is the oldest retained bucket. */
  idx(k: number): number {
    return (this.head - this.count + k + this.cap * 2) % this.cap;
  }

  /** Timeline start of the oldest retained bucket, or `Infinity` when empty. */
  oldest(): number {
    return this.count ? this.t0[this.idx(0)] : Infinity;
  }
}

/** Accumulator for a bucket still being filled. */
interface Accumulator {
  started: boolean;
  t0: number;
  min: number;
  max: number;
  sum: number;
  n: number;
  gap: number;
  rangeMask: number;
  logicAny: number;
  logicAll: number;
  edges: number;
  /** Child buckets folded in so far (coarse tiers only). */
  children: number;
}

function newAccumulator(): Accumulator {
  return {
    started: false, t0: 0, min: Infinity, max: -Infinity, sum: 0, n: 0, gap: 0,
    rangeMask: 0, logicAny: 0, logicAll: 0xff, edges: 0, children: 0,
  };
}

function emit(a: Accumulator): Bucket {
  return {
    t0: a.t0,
    min: a.n ? a.min : 0,
    max: a.n ? a.max : 0,
    sum: a.sum,
    n: a.n,
    gap: a.gap,
    rangeMask: a.rangeMask,
    logicAny: a.logicAny,
    // An empty bucket has no "all channels high" to report; 0xff would claim it did.
    logicAll: a.n ? a.logicAll : 0,
    edges: a.edges,
  };
}

function fold(into: Accumulator, b: Bucket): void {
  if (!into.started) {
    into.started = true;
    into.t0 = b.t0;
  }
  if (b.n > 0) {
    if (b.min < into.min) into.min = b.min;
    if (b.max > into.max) into.max = b.max;
    into.sum += b.sum;
    into.n += b.n;
    into.logicAny |= b.logicAny;
    into.logicAll &= b.logicAll;
  }
  into.gap += b.gap;
  into.rangeMask |= b.rangeMask;
  into.edges = Math.min(65535, into.edges + b.edges);
  into.children++;
}

export class Decimator {
  readonly rings: BucketRing[] = TIERS.map((t) => new BucketRing(t.cap));
  /** Distribution grid over every sample this session has seen. */
  readonly histogram = newHistogram();

  totalStored = 0;
  totalMissing = 0;
  gapCount = 0;

  private accumulators: Accumulator[] = TIERS.map(() => newAccumulator());
  private current = newAccumulator();
  /** Samples plus known-missing samples already placed in the tier-0 bucket. */
  private filled = 0;
  private currentT0: number | null = null;
  private lastLogic = -1;

  /** Feed one sample. `t` is only read when a bucket starts. */
  pushSample(t: number, uA: number, range: number, logic: number): void {
    if (this.currentT0 === null) this.currentT0 = t;
    const a = this.current;
    if (!a.started) {
      a.started = true;
      a.t0 = this.currentT0;
    }
    if (uA < a.min) a.min = uA;
    if (uA > a.max) a.max = uA;
    a.sum += uA;
    a.n++;
    a.rangeMask |= 1 << range;
    a.logicAny |= logic;
    a.logicAll &= logic;
    if (this.lastLogic >= 0) a.edges = Math.min(65535, a.edges + popcount(this.lastLogic ^ logic));
    this.lastLogic = logic;

    this.totalStored++;
    histAdd(this.histogram, uA);
    this.filled++;
    if (this.filled >= SAMPLES_PER_BUCKET) this.closeBucket();
  }

  /** Record `missing` samples that never arrived, spreading them across buckets. */
  pushGap(t: number, missing: number): void {
    if (!(missing > 0)) {
      this.lastLogic = -1;
      return;
    }
    if (this.currentT0 === null) this.currentT0 = t;
    this.totalMissing += missing;
    this.gapCount++;
    let left = missing;
    while (left > 0) {
      // Re-read `this.current` every pass: closeBucket() swaps the accumulator,
      // and a reference captured outside the loop would write the rest of the
      // gap into a bucket that has already been emitted.
      const a = this.current;
      if (!a.started) {
        a.started = true;
        a.t0 = this.currentT0;
      }
      const take = Math.min(SAMPLES_PER_BUCKET - this.filled, left);
      a.gap += take;
      this.filled += take;
      left -= take;
      if (this.filled >= SAMPLES_PER_BUCKET) this.closeBucket();
    }
    // The line was not observed across the gap, so the next sample starts no edge.
    this.lastLogic = -1;
  }

  /**
   * Ingest an already-complete tier-0 bucket.
   *
   * Used to replay pre-recorded history and, in the real client, to accept
   * buckets the server decimated. Counters stay with the caller: one loss spans
   * many buckets, so counting gaps here would report the bucket count instead.
   */
  ingestBucket(b: Bucket): void {
    this.rings[0]!.push(b);
    this.cascade(1, b);
  }

  private closeBucket(): void {
    const b = emit(this.current);
    this.rings[0]!.push(b);
    this.cascade(1, b);
    this.current = newAccumulator();
    this.filled = 0;
    this.currentT0 = (this.currentT0 ?? 0) + TIERS[0]!.ms / 1000;
  }

  private cascade(level: number, b: Bucket): void {
    if (level >= TIERS.length) return;
    const a = this.accumulators[level]!;
    fold(a, b);
    if (a.children >= 10) {
      const out = emit(a);
      this.rings[level]!.push(out);
      this.accumulators[level] = newAccumulator();
      this.cascade(level + 1, out);
    }
  }

  /** Break edge continuity — after a stop, a mode change, or any discontinuity. */
  breakContinuity(): void {
    this.lastLogic = -1;
  }

  /** Fraction of expected samples actually stored, over the whole session. */
  coveredFraction(): number {
    const expected = this.totalStored + this.totalMissing;
    return expected ? this.totalStored / expected : 1;
  }
}
