import { describe, expect, it } from "vitest";

import {
  AXIS_MAX_UA,
  AXIS_MIN_UA,
  CYCLE_SAMPLES,
  FULL_SCALE_UA,
  SAMPLES_PER_BUCKET,
  TIERS,
  rangeFor,
} from "../src/core/constants";
import {
  FULL_RANGE,
  GAP_Y,
  H_LANE,
  MIN_PLOT,
  SUB_BAND,
  chartLayout,
  fitRange,
  hasSubBand,
  yFor,
  zoomRange,
  zoomSpan,
} from "../src/chart/geometry";
import { Decimator, type Bucket } from "../src/core/decimator";
import { modeLabels } from "../src/core/mode";
import { Mode } from "../src/types";
import { fmtCurrent, fmtRelTime } from "../src/core/format";
import { histAdd, histQuantile, newHistogram } from "../src/core/histogram";
import { buildDemoCycle, buildIdleCycle, uartWave } from "../src/core/profiles";

const demo = buildDemoCycle();

describe("simulated profile", () => {
  it("reproduces the documented mean of the shipped --simulate profile", () => {
    let sum = 0;
    for (const v of demo.current) sum += v;
    // docs/faq.md quotes `mean 1806.72 uA` for this profile, measured through
    // the real pipeline. Reproduced here to within the UART wave's rounding.
    expect(sum / CYCLE_SAMPLES).toBeCloseTo(1806.7, 0);
  });

  it("reproduces the documented median", () => {
    const h = newHistogram();
    for (const v of demo.current) histAdd(h, v);
    const p50 = histQuantile(h, 0.5);
    // docs/faq.md quotes `p50 6.05 uA`; the grid resolves it to its bin.
    expect(p50.value).toBeCloseTo(6.05, 1);
    expect(p50.atFloor).toBe(false);
  });

  it("holds the burst, UART and sleep levels", () => {
    expect(demo.current[0]).toBe(12_000);
    expect(demo.current[1600]).toBe(25);
    expect(demo.current[CYCLE_SAMPLES - 1]).toBe(6);
  });

  it("idles D0 and CS high, and toggles D2 at 1 kHz", () => {
    expect(demo.logic[9_999]! & 0b1).toBe(1); // D0 idle high
    expect((demo.logic[9_999]! >> 6) & 1).toBe(1); // D6 (CS) idle high
    expect((demo.logic[0]! >> 2) & 1).toBe(0);
    expect((demo.logic[50]! >> 2) & 1).toBe(1); // flips every 50 samples
  });

  it("gives a UART frame ten samples per bit at 9600 baud", () => {
    const w = uartWave([0x55], 9600);
    expect(w.length).toBe(Math.round(10 * (100_000 / 9600)));
  });
});

describe("decimator", () => {
  /** Feed `cycles` whole profile cycles through the per-sample path. */
  function feed(cycles: number): Decimator {
    const d = new Decimator();
    for (let i = 0; i < CYCLE_SAMPLES * cycles; i++) {
      const j = i % CYCLE_SAMPLES;
      const v = demo.current[j]!;
      d.pushSample(i / 100_000, v, rangeFor(v), demo.logic[j]!);
    }
    return d;
  }

  it("keeps tier-0 buckets exactly equal to a direct computation", () => {
    const d = feed(3);
    const ring = d.rings[0]!;
    expect(ring.count).toBe((CYCLE_SAMPLES * 3) / SAMPLES_PER_BUCKET);
    for (let k = 0; k < ring.count; k++) {
      const bi = ring.idx(k);
      const start = k * SAMPLES_PER_BUCKET;
      let min = Infinity;
      let max = -Infinity;
      let sum = 0;
      for (let t = 0; t < SAMPLES_PER_BUCKET; t++) {
        const v = demo.current[(start + t) % CYCLE_SAMPLES]!;
        if (v < min) min = v;
        if (v > max) max = v;
        sum += v;
      }
      expect(ring.min[bi]).toBeCloseTo(min, 3);
      expect(ring.max[bi]).toBeCloseTo(max, 3);
      expect(ring.sum[bi]).toBeCloseTo(sum, 2);
      expect(ring.n[bi]).toBe(SAMPLES_PER_BUCKET);
    }
  });

  it("folds each coarse bucket from exactly ten of the tier below", () => {
    const d = feed(3);
    const fine = d.rings[0]!;
    const coarse = d.rings[1]!;
    expect(coarse.count).toBe(fine.count / 10);
    for (let k = 0; k < coarse.count; k++) {
      const ci = coarse.idx(k);
      let min = Infinity;
      let max = -Infinity;
      let sum = 0;
      let n = 0;
      for (let t = 0; t < 10; t++) {
        const fi = fine.idx(k * 10 + t);
        if (fine.min[fi] < min) min = fine.min[fi]!;
        if (fine.max[fi] > max) max = fine.max[fi]!;
        sum += fine.sum[fi]!;
        n += fine.n[fi]!;
      }
      expect(coarse.min[ci]).toBeCloseTo(min, 3);
      expect(coarse.max[ci]).toBeCloseTo(max, 3);
      expect(coarse.sum[ci]).toBeCloseTo(sum, 2);
      expect(coarse.n[ci]).toBe(n);
    }
  });

  it("accounts for every lost sample when a gap spans several buckets", () => {
    // Regression: an earlier version captured `this.current` outside the loop,
    // so a gap wider than the room left in the open bucket wrote the remainder
    // into an accumulator that had already been emitted. 320 injected samples
    // were recorded as 50.
    const d = new Decimator();
    for (let i = 0; i < 250; i++) d.pushSample(i / 100_000, 6, 0, 0);
    d.pushGap(250 / 100_000, 320);
    for (let i = 0; i < 250; i++) d.pushSample((500 + i) / 100_000, 6, 0, 0);

    const ring = d.rings[0]!;
    let inBuckets = 0;
    for (let k = 0; k < ring.count; k++) inBuckets += ring.gap[ring.idx(k)]!;
    // The gap opens with 50 samples of room left, then fills three more buckets
    // and leaves 70 in the open one; the samples that follow close that bucket
    // too. Every lost sample therefore reaches a bucket, and the running total
    // agrees with the buckets rather than being tracked separately.
    expect(inBuckets).toBe(320);
    expect(d.totalMissing).toBe(320);
    expect(d.gapCount).toBe(1);
  });

  it("counts one gap event however many buckets the loss spans", () => {
    const d = new Decimator();
    d.pushGap(0, 5000); // 50 buckets' worth
    expect(d.gapCount).toBe(1);
    expect(d.totalMissing).toBe(5000);
  });

  it("reports coverage over stored and missing together", () => {
    const d = new Decimator();
    for (let i = 0; i < 900; i++) d.pushSample(i / 100_000, 6, 0, 0);
    d.pushGap(0.009, 100);
    expect(d.coveredFraction()).toBeCloseTo(0.9, 6);
  });

  it("does not invent an all-high logic byte for an empty bucket", () => {
    const d = new Decimator();
    d.pushGap(0, SAMPLES_PER_BUCKET);
    const ring = d.rings[0]!;
    expect(ring.count).toBe(1);
    const i = ring.idx(0);
    expect(ring.n[i]).toBe(0);
    expect(ring.logicAll[i]).toBe(0);
    expect(ring.gap[i]).toBe(SAMPLES_PER_BUCKET);
  });

  it("accepts pre-decimated buckets without double-counting gap events", () => {
    const d = new Decimator();
    const b: Bucket = {
      t0: 0, min: 6, max: 6, sum: 600, n: 100, gap: 0,
      rangeMask: 1, logicAny: 0, logicAll: 0, edges: 0,
    };
    for (let i = 0; i < 25; i++) d.ingestBucket({ ...b, t0: i / 1000 });
    expect(d.rings[0]!.count).toBe(25);
    expect(d.rings[1]!.count).toBe(2); // 10 fine buckets per coarse one
    expect(d.gapCount).toBe(0);
  });
});

describe("distribution grid", () => {
  it("marks a quantile served from the 200 nA floor as a bound, not a reading", () => {
    const idle = buildIdleCycle();
    const h = newHistogram();
    for (const v of idle.current) histAdd(h, v);
    const p50 = histQuantile(h, 0.5);
    expect(p50.atFloor).toBe(true);
    expect(p50.value).toBe(0.2);
  });

  it("puts zero and negative readings in the underflow bin", () => {
    const h = newHistogram();
    histAdd(h, -0.5);
    histAdd(h, 0);
    histAdd(h, 0.15);
    expect(h[0]).toBe(3);
  });

  it("returns null rather than a number for an empty grid", () => {
    expect(histQuantile(newHistogram(), 0.5).value).toBeNull();
  });
});

describe("ranges and formatting", () => {
  it("picks the smallest range covering the current", () => {
    expect(rangeFor(6)).toBe(0);
    expect(rangeFor(50)).toBe(0);
    expect(rangeFor(50.1)).toBe(1);
    expect(rangeFor(12_000)).toBe(3);
    expect(rangeFor(FULL_SCALE_UA[4])).toBe(4);
    expect(rangeFor(2_000_000)).toBe(4); // beyond full scale still lands in range 4
  });

  it("never rounds a negative reading away", () => {
    expect(fmtCurrent(-0.2477)).toBe("-247.7 nA");
    expect(fmtCurrent(0)).toBe("0.0 nA");
  });

  it("scales the unit with the magnitude", () => {
    expect(fmtCurrent(6)).toBe("6.000 µA");
    expect(fmtCurrent(12_000)).toBe("12.000 mA");
    expect(fmtCurrent(1_000_000)).toBe("1.000 A");
  });

  it("signs relative times with a typographic minus", () => {
    expect(fmtRelTime(-1)).toBe("−1.000s");
    expect(fmtRelTime(-90)).toBe("−1m30.00s");
  });

  it("has a tier ladder where each step is ten of the one below", () => {
    for (let i = 1; i < TIERS.length; i++) {
      expect(TIERS[i]!.ms).toBe(TIERS[i - 1]!.ms * 10);
    }
  });
});

describe("chart layout", () => {
  it("spends every spare pixel on the plot, not on the fixed rows", () => {
    const short = chartLayout(400, true);
    const tall = chartLayout(900, true);
    expect(tall.plot - short.plot).toBe(500);
    expect(tall.total - short.total).toBe(500);
    // The lanes, the range strip and the axis are legible at one size.
    expect(tall.axisY - tall.digitalY!).toBe(short.axisY - short.digitalY!);
  });

  it("reclaims the lane block when the digital view is off", () => {
    const withLanes = chartLayout(600, true);
    const without = chartLayout(600, false);
    expect(without.digitalY).toBeNull();
    expect(without.plot - withLanes.plot).toBe(H_LANE * 8 + GAP_Y);
  });

  it("stops shrinking the plot once the decades stop being separable", () => {
    const tiny = chartLayout(80, true);
    expect(tiny.plot).toBe(MIN_PLOT);
    // Clamping means the canvas is taller than offered; the shell clips rather
    // than letting a 20px plot claim to show seven decades.
    expect(tiny.total).toBeGreaterThan(80);
  });

  it("drops the sub-band once the axis no longer reaches its floor", () => {
    const l = chartLayout(600, true);
    const zoomed = { lo: 100, hi: 100_000 };
    expect(hasSubBand(zoomed)).toBe(false);
    // Zoomed in, the bottom of the plot is the bottom of the range itself.
    expect(yFor(100, "log", l.plot, zoomed)).toBe(l.plot - 2);
    expect(yFor(100_000, "log", l.plot, zoomed)).toBeCloseTo(6, 6);
  });

  it("fits outward to whole decades so nothing sits on an edge", () => {
    expect(fitRange(6, 12_000)).toEqual({ lo: 1, hi: 100_000 });
    // A flat signal still gets a decade to live in rather than a zero-height axis.
    expect(fitRange(6, 6).hi).toBeGreaterThan(fitRange(6, 6).lo);
    expect(fitRange(Infinity, Infinity)).toEqual(FULL_RANGE);
  });

  it("never zooms the current axis below one decade or outside the device", () => {
    const tight = zoomRange({ lo: 1000, hi: 10_000 }, 0.1, 3000);
    expect(Math.log10(tight.hi) - Math.log10(tight.lo)).toBeCloseTo(1, 6);
    const wide = zoomRange(FULL_RANGE, 10, 1000);
    expect(wide).toEqual(FULL_RANGE);
  });

  it("keeps the instant under the cursor fixed while zooming time", () => {
    // Cursor at the right edge: zooming must not move the right edge.
    const atEdge = zoomSpan(10, 100, 0.5, 100, 600);
    expect(atEdge.end).toBeCloseTo(100, 9);
    expect(atEdge.span).toBe(5);
    // Cursor in the middle: the pivot instant stays where it was.
    const mid = zoomSpan(10, 100, 0.5, 95, 600);
    expect(mid.end - mid.span * ((mid.end - 95) / mid.span)).toBeCloseTo(95, 9);
    // Clamped to the history actually retained.
    expect(zoomSpan(10, 100, 100, 95, 600).span).toBe(600);
  });

  it("keeps the sub-band out of the log mapping", () => {
    const l = chartLayout(600, true);
    // The axis floor belongs to the axis, not to the band below it.
    expect(yFor(AXIS_MIN_UA, "log", l.plot)).toBe(l.plot - SUB_BAND);
    // Zero and negatives are placed inside the band, never clipped away.
    expect(yFor(0, "log", l.plot)).toBe(l.plot - SUB_BAND / 2);
    expect(yFor(-0.25, "log", l.plot)).toBe(l.plot - SUB_BAND / 2);
    // Full scale sits at the top.
    expect(yFor(AXIS_MAX_UA, "log", l.plot)).toBeCloseTo(6, 6);
  });

  it("maps the linear scale from zero across the whole plot", () => {
    const l = chartLayout(600, true);
    // A linear axis reaching the device floor starts at zero, not at 100 nA.
    expect(yFor(0, "lin", l.plot)).toBe(l.plot - 2);
    expect(yFor(AXIS_MAX_UA, "lin", l.plot)).toBe(6);
  });
});

describe("measurement mode labels", () => {
  it("never renders an unknown mode as a known one", () => {
    // `DeviceState.mode` is `Mode | null` and the null means unknown, never a
    // default. Collapsing it with `mode === Mode.SOURCE` made unknown render
    // as a confident "Ampere Meter" -- and the mode decides whether energy is
    // computable at all, so the guess propagates into an energy story with
    // nothing behind it.
    const unknown = modeLabels(null);
    expect(unknown.known).toBe(false);
    expect(unknown.badge).toBe("UNKNOWN");
    expect(unknown.name).not.toBe(modeLabels(Mode.AMPERE).name);
    expect(unknown.name).not.toBe(modeLabels(Mode.SOURCE).name);
  });

  it("tones an unknown mode like the DUT-power cell beside it", () => {
    expect(modeLabels(null).tone).toBe("unk");
    expect(modeLabels(Mode.SOURCE).tone).toBe("acc");
    expect(modeLabels(Mode.AMPERE).tone).toBe("acc");
  });

  it("labels the two known modes distinctly", () => {
    expect(modeLabels(Mode.SOURCE).badge).toBe("SOURCE");
    expect(modeLabels(Mode.AMPERE).badge).toBe("AMPERE");
  });
});
