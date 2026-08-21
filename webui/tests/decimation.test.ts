import { describe, expect, it } from "vitest";

import { SAMPLE_RATE_HZ, SAMPLES_PER_BUCKET } from "../src/core/constants";
import { Decimator } from "../src/core/decimator";
import { histAdd, newHistogram } from "../src/core/histogram";
import goldenJson from "../../tests/vectors/decimation-golden.json";

/**
 * The console and the server decimate the same samples in two languages, and a
 * live trace is only trustworthy if they agree bucket for bucket. Each suite is
 * internally consistent no matter what the other side does, so nothing but a
 * shared vector can catch a drift between them.
 *
 * `tests/vectors/decimation-golden.json` carries the input -- explicit
 * (microamps, range, logic) triples and gap lengths -- and the buckets the
 * Python `Tier0Accumulator` produced from it. This drives the same input
 * through `Decimator` and compares. Regenerate with
 * `python tests/vectors/generate_decimation_golden.py` and read the diff: a change to
 * the vector is a change to a contract two languages share.
 */
interface GoldenBucket {
  start_index: number;
  t0: number;
  min: number;
  max: number;
  sum: number;
  n: number;
  gap: number;
  excluded: number;
  rangeMask: number;
  logicAny: number;
  logicAll: number;
  edges: number;
}

type GoldenEvent =
  | { kind: "samples"; ua: number[]; range: number[]; logic: number[] }
  | { kind: "gap"; missing: number };

interface Golden {
  samples_per_bucket: number;
  events: GoldenEvent[];
  buckets: GoldenBucket[];
  counters: {
    total_stored: number;
    total_missing: number;
    total_excluded: number;
    gap_count: number;
  };
  histogram_nonzero: Record<string, number>;
}

// Imported rather than read from disk: it keeps the test free of @types/node,
// and Vite resolves the path the same way in `vitest` and in a build.
const golden = goldenJson as unknown as Golden;

function replay(): Decimator {
  const dec = new Decimator();
  let index = 0;
  for (const event of golden.events) {
    if (event.kind === "gap") {
      dec.pushGap(index / SAMPLE_RATE_HZ, event.missing);
      index += event.missing;
      continue;
    }
    for (let i = 0; i < event.ua.length; i++) {
      dec.pushSample(index / SAMPLE_RATE_HZ, event.ua[i]!, event.range[i]!, event.logic[i]!);
      index++;
    }
  }
  return dec;
}

/**
 * Buckets oldest-first.
 *
 * The ring holds parallel typed arrays rather than objects -- at 1000 buckets
 * per second that is what a long session would otherwise pay for in allocation
 * -- so `idx(k)` maps logical position to physical slot.
 */
function closedBuckets(dec: Decimator): GoldenBucket[] {
  const ring = dec.rings[0]!;
  const out: GoldenBucket[] = [];
  for (let k = 0; k < ring.count; k++) {
    const i = ring.idx(k);
    out.push({
      start_index: 0, // not carried by the ring; see the t0 test
      t0: ring.t0[i]!,
      min: ring.min[i]!,
      max: ring.max[i]!,
      sum: ring.sum[i]!,
      n: ring.n[i]!,
      gap: ring.gap[i]!,
      excluded: 0,
      rangeMask: ring.rangeMask[i]!,
      logicAny: ring.logicAny[i]!,
      logicAll: ring.logicAll[i]!,
      edges: ring.edges[i]!,
    });
  }
  return out;
}

describe("decimation agrees with the Python server", () => {
  it("has a vector free of unusable samples, which this side cannot represent", () => {
    // The console's Bucket has two bins, `n` and `gap`. The server adds a third
    // for samples that arrived and cannot be trusted, and it has no counterpart
    // here -- so the vector is deliberately generated from a calibrated device,
    // where the column is identically zero and the two sides are comparable.
    expect(golden.counters.total_excluded).toBe(0);
    expect(golden.buckets.every((b) => b.excluded === 0)).toBe(true);
    expect(golden.samples_per_bucket).toBe(SAMPLES_PER_BUCKET);
  });

  it("closes the same buckets, with the same contents", () => {
    const got = closedBuckets(replay());
    expect(got.length).toBe(golden.buckets.length);
    for (let i = 0; i < got.length; i++) {
      const want = golden.buckets[i]!;
      const mine = got[i]!;
      expect({ i, ...mine, t0: 0, start_index: 0 }).toEqual({
        i,
        ...want,
        t0: 0,
        start_index: 0,
      });
    }
  });

  it("puts each bucket at the same point on the timeline", () => {
    // Compared with a tolerance rather than exactly, and the reason is worth
    // recording: this side advances `currentT0` by adding 1 ms per bucket, so
    // it accumulates float error, while the server divides an integer sample
    // index by the sample rate. The server's is the exact one, which is why the
    // wire carries `start_index` and `WebSocketSource` derives t0 from it by
    // integer subtraction rather than trusting an accumulated float.
    const got = closedBuckets(replay());
    for (let i = 0; i < got.length; i++) {
      expect(got[i]!.t0).toBeCloseTo(golden.buckets[i]!.t0, 9);
      expect(golden.buckets[i]!.t0).toBeCloseTo(
        golden.buckets[i]!.start_index / SAMPLE_RATE_HZ,
        12,
      );
    }
  });

  it("keeps the same running counters", () => {
    const dec = replay();
    expect(dec.totalStored).toBe(golden.counters.total_stored);
    expect(dec.totalMissing).toBe(golden.counters.total_missing);
    // One loss spans many buckets, so a gap is counted once per event.
    expect(dec.gapCount).toBe(golden.counters.gap_count);
  });

  it("bins every sample into the same distribution grid", () => {
    // The one place a difference is tolerated: V8's Math.log10 and CPython's
    // libm are separate implementations and are not guaranteed identical at
    // every ULP, so a value landing exactly on a bin edge could fall either
    // side. Nothing in this vector sits on an edge, so equality is expected --
    // and a failure here means a real divergence, not a rounding artefact.
    const dec = replay();
    const nonzero: Record<string, number> = {};
    dec.histogram.forEach((value, i) => {
      if (value) nonzero[String(i)] = value;
    });
    expect(nonzero).toEqual(golden.histogram_nonzero);
  });

  it("counts an edge across a block seam", () => {
    // The vector's first two sample runs are 137 and 213 long, so the seam
    // falls mid-bucket. A per-block reset of the edge state would lose exactly
    // one transition there, which is invisible in aggregate and wrong.
    const seamBucket = golden.buckets[1]!;
    expect(seamBucket.edges).toBeGreaterThan(0);
  });
});

describe("the console's distribution grid", () => {
  it("puts NaN in the underflow bin, as the server does", () => {
    const hist = newHistogram();
    histAdd(hist, NaN);
    expect(hist[0]).toBe(1);
  });

  it("puts a genuine negative reading in the underflow bin", () => {
    // Negative currents are legitimate on a shunt and must not be dropped.
    const hist = newHistogram();
    histAdd(hist, -0.2477);
    expect(hist[0]).toBe(1);
  });
});
