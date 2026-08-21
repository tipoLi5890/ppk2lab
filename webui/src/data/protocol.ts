import type { Bucket } from "../core/decimator";

/**
 * The binary half of the wire, decoded.
 *
 * Mirrors `src/ppk2lab_web/protocol.py` field for field. Text frames on the
 * same socket are JSON and carry everything a person acts on; samples are
 * binary for three reasons, in order of weight: the server's converter
 * produces NaN for a range with no calibration constants and JSON cannot carry
 * NaN; tier 0 is a thousand buckets a second, which is 24 kB/s packed against
 * roughly 130 kB/s and a thousand object allocations as JSON; and the layout is
 * struct-of-arrays, so each section becomes a typed-array view over the
 * received buffer rather than a thousand JavaScript numbers.
 *
 * Every decoder here **throws** on a frame this build did not expect. That is
 * the important property: a changed layout read as numbers is a fabricated
 * current trace, and a console that draws one is worse than a console that
 * refuses to start.
 */
export const PROTOCOL_VERSION = 1;

export const TAG_BUCKETS = 0x01;
export const TAG_HISTOGRAM = 0x02;

const FLAG_DISCONTINUITY = 1 << 0;
const BUCKET_HEADER = 16;
const HISTOGRAM_HEADER = 8;
/** 8 + 4 + 4 + 2 + six single bytes. */
export const BUCKET_STRIDE = 24;

/**
 * Every platform a browser runs on is little-endian, and the frames say so
 * explicitly. Asserted once rather than assumed, because the failure mode of
 * being wrong is a garbled trace rather than an error.
 */
export const HOST_IS_LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

export interface BucketBatch {
  kind: "buckets";
  count: number;
  /** Sample index of the first bucket. Later ones are derived from the widths. */
  firstIndex: number;
  /** The first bucket does not continue the one before it. */
  discontinuity: boolean;
  sum: Float64Array;
  min: Float32Array;
  max: Float32Array;
  edges: Uint16Array;
  n: Uint8Array;
  gap: Uint8Array;
  excluded: Uint8Array;
  rangeMask: Uint8Array;
  logicAny: Uint8Array;
  logicAll: Uint8Array;
}

export interface HistogramFrame {
  kind: "histogram";
  bins: Float64Array;
}

export type BinaryFrame = BucketBatch | HistogramFrame;

export function decodeFrame(buffer: ArrayBuffer): BinaryFrame {
  if (buffer.byteLength < 2) throw new Error("binary frame shorter than its tag");
  const head = new DataView(buffer);
  const tag = head.getUint8(0);
  const version = head.getUint8(1);
  if (version !== PROTOCOL_VERSION) {
    throw new Error(`frame speaks protocol ${version}, not ${PROTOCOL_VERSION}`);
  }
  if (tag === TAG_BUCKETS) return decodeBuckets(buffer, head);
  if (tag === TAG_HISTOGRAM) return decodeHistogram(buffer, head);
  throw new Error(`unknown frame tag 0x${tag.toString(16)}`);
}

function decodeBuckets(buffer: ArrayBuffer, head: DataView): BucketBatch {
  if (buffer.byteLength < BUCKET_HEADER) throw new Error("bucket frame shorter than its header");
  const count = head.getUint16(2, true);
  const flags = head.getUint32(4, true);
  // Sample indices are exact as doubles until 2^53, which at 100 kS/s is about
  // 2850 years of streaming. Reading it as a BigInt and narrowing keeps the
  // conversion explicit rather than assembling two halves by hand.
  const firstIndex = Number(head.getBigUint64(8, true));
  const expected = BUCKET_HEADER + BUCKET_STRIDE * count;
  if (buffer.byteLength !== expected) {
    throw new Error(`bucket frame is ${buffer.byteLength} bytes, expected ${expected}`);
  }

  // Sections run in descending alignment, so every one of these views starts on
  // its own boundary for any count and none of them copies.
  let off = BUCKET_HEADER;
  const sum = new Float64Array(buffer, off, count);
  off += 8 * count;
  const min = new Float32Array(buffer, off, count);
  off += 4 * count;
  const max = new Float32Array(buffer, off, count);
  off += 4 * count;
  const edges = new Uint16Array(buffer, off, count);
  off += 2 * count;
  const n = new Uint8Array(buffer, off, count);
  off += count;
  const gap = new Uint8Array(buffer, off, count);
  off += count;
  const excluded = new Uint8Array(buffer, off, count);
  off += count;
  const rangeMask = new Uint8Array(buffer, off, count);
  off += count;
  const logicAny = new Uint8Array(buffer, off, count);
  off += count;
  const logicAll = new Uint8Array(buffer, off, count);

  return {
    kind: "buckets",
    count,
    firstIndex,
    discontinuity: (flags & FLAG_DISCONTINUITY) !== 0,
    sum,
    min,
    max,
    edges,
    n,
    gap,
    excluded,
    rangeMask,
    logicAny,
    logicAll,
  };
}

function decodeHistogram(buffer: ArrayBuffer, head: DataView): HistogramFrame {
  if (buffer.byteLength < HISTOGRAM_HEADER) {
    throw new Error("histogram frame shorter than its header");
  }
  const bins = head.getUint16(2, true);
  const expected = HISTOGRAM_HEADER + 8 * bins;
  if (buffer.byteLength !== expected) {
    throw new Error(`histogram frame is ${buffer.byteLength} bytes, expected ${expected}`);
  }
  return { kind: "histogram", bins: new Float64Array(buffer, HISTOGRAM_HEADER, bins) };
}

/**
 * Walk a batch, filling one reusable `Bucket` per step.
 *
 * The object is deliberately reused: `BucketRing.push` copies every field into
 * its typed arrays and `fold` only reads, so nothing retains it. At a thousand
 * buckets a second that is the difference between zero allocations and a
 * thousand short-lived objects.
 *
 * `sum` is carried rather than a mean, and `t0` is derived from the sample
 * index rather than sent: buckets are contiguous on the timeline, so each one
 * starts where the last ended, and integer arithmetic all the way to the screen
 * is what keeps the trace from slowly disagreeing with its own timestamps.
 */
export function eachBucket(
  batch: BucketBatch,
  attachIndex: number,
  sampleRateHz: number,
  visit: (bucket: Bucket, startIndex: number) => void,
): number {
  const scratch: Bucket = {
    t0: 0,
    min: 0,
    max: 0,
    sum: 0,
    n: 0,
    gap: 0,
    rangeMask: 0,
    logicAny: 0,
    logicAll: 0,
    edges: 0,
  };
  let index = batch.firstIndex;
  for (let i = 0; i < batch.count; i++) {
    scratch.t0 = (index - attachIndex) / sampleRateHz;
    scratch.min = batch.min[i]!;
    scratch.max = batch.max[i]!;
    scratch.sum = batch.sum[i]!;
    scratch.n = batch.n[i]!;
    // The console's Bucket has no bin for a sample that arrived and cannot be
    // trusted. Counting it as a gap would accuse the instrument of loss it did
    // not commit, so it is left out of both and surfaced through the counters.
    scratch.gap = batch.gap[i]!;
    scratch.rangeMask = batch.rangeMask[i]!;
    scratch.logicAny = batch.logicAny[i]!;
    scratch.logicAll = batch.logicAll[i]!;
    scratch.edges = batch.edges[i]!;
    visit(scratch, index);
    index += batch.n[i]! + batch.gap[i]! + batch.excluded[i]!;
  }
  return index;
}

/** An empty bucket standing for a stretch this console never received. */
export function missingBucket(
  startIndex: number,
  attachIndex: number,
  sampleRateHz: number,
  width: number,
): Bucket {
  return {
    t0: (startIndex - attachIndex) / sampleRateHz,
    min: 0,
    max: 0,
    sum: 0,
    n: 0,
    gap: width,
    rangeMask: 0,
    logicAny: 0,
    logicAll: 0,
    edges: 0,
  };
}
