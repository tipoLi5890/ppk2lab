import { beforeEach, describe, expect, it, vi } from "vitest";

import { Link, LinkClosed, type LinkOptions, type WebSocketLike } from "../src/data/link";
import { BUCKET_STRIDE, PROTOCOL_VERSION, decodeFrame } from "../src/data/protocol";
import { resolveSource, websocketUrl } from "../src/data/select";
import { WebSocketSource } from "../src/data/websocket";

/**
 * A bucket frame, built here rather than by the decoder's own helpers.
 *
 * Deliberately a second implementation of the layout: a round trip through one
 * codec proves only that it is self-consistent, and the thing that matters is
 * that it agrees with `src/ppk2lab_web/protocol.py`. This mirrors that packer
 * field for field.
 */
function packBuckets(
  buckets: Array<{
    sum: number;
    min: number;
    max: number;
    edges: number;
    n: number;
    gap: number;
    excluded: number;
    rangeMask: number;
    logicAny: number;
    logicAll: number;
  }>,
  firstIndex: number,
  discontinuity = false,
): ArrayBuffer {
  const count = buckets.length;
  const buffer = new ArrayBuffer(16 + BUCKET_STRIDE * count);
  const view = new DataView(buffer);
  view.setUint8(0, 0x01);
  view.setUint8(1, PROTOCOL_VERSION);
  view.setUint16(2, count, true);
  view.setUint32(4, discontinuity ? 1 : 0, true);
  view.setBigUint64(8, BigInt(firstIndex), true);

  let off = 16;
  for (const b of buckets) view.setFloat64(off, b.sum, true), (off += 8);
  for (const b of buckets) view.setFloat32(off, b.min, true), (off += 4);
  for (const b of buckets) view.setFloat32(off, b.max, true), (off += 4);
  for (const b of buckets) view.setUint16(off, b.edges, true), (off += 2);
  for (const b of buckets) view.setUint8(off++, b.n);
  for (const b of buckets) view.setUint8(off++, b.gap);
  for (const b of buckets) view.setUint8(off++, b.excluded);
  for (const b of buckets) view.setUint8(off++, b.rangeMask);
  for (const b of buckets) view.setUint8(off++, b.logicAny);
  for (const b of buckets) view.setUint8(off++, b.logicAll);
  return buffer;
}

function packHistogram(bins: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(8 + 8 * bins.length);
  const view = new DataView(buffer);
  view.setUint8(0, 0x02);
  view.setUint8(1, PROTOCOL_VERSION);
  view.setUint16(2, bins.length, true);
  bins.forEach((v, i) => view.setFloat64(8 + 8 * i, v, true));
  return buffer;
}

const BUCKET = {
  sum: 1234.5,
  min: -0.25,
  max: 12000.5,
  edges: 7,
  n: 100,
  gap: 0,
  excluded: 0,
  rangeMask: 0b1001,
  logicAny: 0xa5,
  logicAll: 0x05,
};

describe("the binary frame", () => {
  it("decodes every field the server packs", () => {
    const frame = decodeFrame(packBuckets([BUCKET, { ...BUCKET, n: 40, gap: 60 }], 500, true));
    expect(frame.kind).toBe("buckets");
    if (frame.kind !== "buckets") return;
    expect(frame.count).toBe(2);
    expect(frame.firstIndex).toBe(500);
    expect(frame.discontinuity).toBe(true);
    expect(frame.sum[0]).toBe(1234.5);
    expect(frame.min[0]).toBe(-0.25);
    expect(frame.max[0]).toBe(12000.5);
    expect([...frame.n]).toEqual([100, 40]);
    expect([...frame.gap]).toEqual([0, 60]);
    expect([...frame.logicAll]).toEqual([5, 5]);
  });

  it("carries NaN, which is the reason this plane is not JSON", () => {
    const frame = decodeFrame(packBuckets([{ ...BUCKET, sum: NaN, min: NaN }], 0));
    if (frame.kind !== "buckets") throw new Error("wrong kind");
    expect(Number.isNaN(frame.sum[0]!)).toBe(true);
    expect(Number.isNaN(frame.min[0]!)).toBe(true);
  });

  it("throws rather than decoding a frame this build did not expect", () => {
    // A decoder that guesses turns a protocol mismatch into a plausible current
    // trace, which is the one failure this project exists to prevent.
    const good = packBuckets([BUCKET], 0);
    const wrongVersion = good.slice(0);
    new DataView(wrongVersion).setUint8(1, PROTOCOL_VERSION + 1);
    expect(() => decodeFrame(wrongVersion)).toThrow(/protocol/);

    const wrongTag = good.slice(0);
    new DataView(wrongTag).setUint8(0, 0x09);
    expect(() => decodeFrame(wrongTag)).toThrow(/unknown frame tag/);

    expect(() => decodeFrame(good.slice(0, good.byteLength - 1))).toThrow(/expected/);
    expect(() => decodeFrame(new ArrayBuffer(1))).toThrow();
  });

  it("decodes a histogram and keeps it exact", () => {
    // An eight-hour session is 2.9e9 samples; float32 stops counting exactly
    // above 2^24, so the grid is float64 on the wire.
    const frame = decodeFrame(packHistogram([2, 1e9, 5]));
    if (frame.kind !== "histogram") throw new Error("wrong kind");
    expect([...frame.bins]).toEqual([2, 1e9, 5]);
  });
});

describe("choosing a source", () => {
  it("defaults to the page's own origin, with no host in the bundle", () => {
    // The build is committed into the Python wheel and shipped to PyPI, so a
    // host baked in here would be permanently wrong for every user.
    const url = websocketUrl(new URL("http://192.168.1.9:8765/"));
    expect(url).toBe("ws://192.168.1.9:8765/ws");
    expect(url).not.toContain("localhost");
  });

  it("uses wss when the page is served over https", () => {
    expect(websocketUrl(new URL("https://bench.local/"))).toBe("wss://bench.local/ws");
  });

  it("lets a page point at another bench without a rebuild", () => {
    expect(websocketUrl(new URL("http://x/?ws=wss://pi.local:9/ws"))).toBe("wss://pi.local:9/ws");
  });

  it("runs the simulator in development and the socket in production", () => {
    expect(resolveSource(new URL("http://x/"), true)).toEqual({ kind: "simulated" });
    expect(resolveSource(new URL("http://x/"), false).kind).toBe("websocket");
    expect(resolveSource(new URL("http://x/?source=sim"), false)).toEqual({ kind: "simulated" });
    expect(resolveSource(new URL("http://x/?source=ws"), true).kind).toBe("websocket");
  });
});

class FakeSocket implements WebSocketLike {
  static instances: FakeSocket[] = [];
  binaryType = "";
  readyState = 1;
  sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.onclose?.({ code: 1000, reason: "" });
  }
  open(): void {
    this.onopen?.({});
  }
  deliver(message: unknown): void {
    this.onmessage?.({ data: typeof message === "string" ? message : message });
  }
  drop(code = 1006): void {
    this.onclose?.({ code, reason: "" });
  }
}

function linkOptions(overrides: Partial<LinkOptions> = {}): LinkOptions {
  return {
    url: "ws://test/ws",
    socketFactory: (url: string) => new FakeSocket(url),
    now: () => 1000,
    random: () => 0.5,
    ...overrides,
  };
}

describe("the link", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.useFakeTimers();
  });

  it("keeps exactly one socket alive while it retries", () => {
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    expect(FakeSocket.instances).toHaveLength(1);
    FakeSocket.instances[0]!.drop();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(2);
    link.close();
  });

  it("does not come back from a refusal no retry can fix", () => {
    // 1008 is the origin check. Retrying it forever is noise, and the console
    // has something specific to say instead.
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    FakeSocket.instances[0]!.drop(1008);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(link.phase).toBe("closed");
  });

  it("answers a request that was sent and never acknowledged, and never retries it", async () => {
    // The console must be told the outcome is *unknown*, not that it failed:
    // replaying a state change that may already have landed is not safe.
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    link.markEstablished();
    const socket = FakeSocket.instances[0]!;
    const inflight = link.request("apply", {}, 5000);
    expect(socket.sent).toHaveLength(1);
    socket.drop();
    await expect(inflight).rejects.toBeInstanceOf(LinkClosed);
    await expect(inflight).rejects.toMatchObject({ reasonKey: "er_disconnected" });
    vi.advanceTimersByTime(1000);
    // The retry opens a socket; it does not resend the command.
    expect(FakeSocket.instances[1]!.sent).toHaveLength(0);
    link.close();
  });

  it("says nothing was sent when nothing was sent", async () => {
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    await expect(link.request("apply", {}, 5000)).rejects.toMatchObject({
      reasonKey: "er_offline",
    });
    link.close();
  });

  it("times out with the outcome marked unknown", async () => {
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    link.markEstablished();
    const inflight = link.request("apply", {}, 5000);
    vi.advanceTimersByTime(5001);
    await expect(inflight).rejects.toMatchObject({ reasonKey: "er_timeout" });
    link.close();
  });

  it("resets its backoff only once a link is usable", () => {
    // A server that accepts and immediately closes -- wrong protocol, port
    // busy -- would otherwise be reconnected to in a hot loop.
    const link = new Link(linkOptions(), { onJson: () => {}, onBinary: () => {}, onPhase: () => {} });
    link.start();
    FakeSocket.instances[0]!.drop();
    expect(link.attempt).toBe(1);
    vi.advanceTimersByTime(1000);
    FakeSocket.instances[1]!.drop();
    expect(link.attempt).toBe(2);
    vi.advanceTimersByTime(2000);
    link.markEstablished();
    expect(link.attempt).toBe(0);
    link.close();
  });
});

describe("the websocket source", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.useFakeTimers();
  });

  function connect() {
    const source = new WebSocketSource(linkOptions());
    const socket = FakeSocket.instances[0]!;
    socket.open();
    socket.deliver(
      JSON.stringify({
        type: "hello",
        protocol: PROTOCOL_VERSION,
        session: { control_allowed: true, control_token: "t", attach_index: 0, bucket_us: 1000 },
        device: { simulated: true, serial_number: "SIM0001" },
        // Exactly what `DeviceState.to_json()` emits, so this fixture cannot
        // drift into a shape the server never sends.
        state: {
          mode: "source",
          source_voltage_mv: 3000,
          dut_power: false,
          measuring: true,
          source_voltage_basis: "configured_source",
        },
        calibration: { calibrated: true, ranges: [], missing_ranges: [], terminated: true },
        limits: { max_voltage_mv: 3600 },
        counters: {},
        streaming: true,
        state_seq: 1,
        warnings: [],
      }),
    );
    return { source, socket };
  }

  it("reads the mode the library actually serialises", () => {
    // `DeviceState.to_json()` emits "source" / "ampere" -- the frozen shape
    // `ppk2lab info --json` publishes -- while the console carries the numeric
    // wire value. Taking the string at face value rendered a known mode as
    // UNKNOWN, and mode is what decides whether energy is computable at all.
    const { source } = connect();
    expect(source.snapshot().state.mode).toBe(2);
    expect(source.config().mode).toBe(2);
  });

  it("leaves a mode it does not recognise unknown rather than guessing", () => {
    const source = new WebSocketSource(linkOptions());
    const socket = FakeSocket.instances[0]!;
    socket.open();
    socket.deliver(
      JSON.stringify({
        type: "hello",
        protocol: PROTOCOL_VERSION,
        session: { control_allowed: false, attach_index: 0 },
        device: {},
        state: { mode: "something-new", measuring: false },
        limits: {},
      }),
    );
    expect(source.snapshot().state.mode).toBeNull();
  });

  it("is referentially stable, which is the difference between a render and a loop", () => {
    const { source } = connect();
    expect(source.snapshot()).toBe(source.snapshot());
    expect(source.events()).toBe(source.events());
  });

  it("refuses a server whose protocol it does not speak, before any sample", () => {
    const source = new WebSocketSource(linkOptions());
    FakeSocket.instances[0]!.open();
    FakeSocket.instances[0]!.deliver(
      JSON.stringify({ type: "hello", protocol: PROTOCOL_VERSION + 1, session: {}, device: {} }),
    );
    expect(source.snapshot().connection.phase).toBe("closed");
    expect(source.snapshot().connection.reasonKey).toBe("conn_version");
  });

  it("never draws past the newest data it has", () => {
    // Drawing past the edge leaves a blank strip that renders as "the DUT drew
    // nothing" -- a measurement that was never taken.
    const { source, socket } = connect();
    socket.deliver(packBuckets([BUCKET, BUCKET], 0));
    source.tick(0);
    for (let t = 16; t < 5000; t += 16) source.tick(t);
    expect(source.now()).toBeLessThanOrEqual(0.002);
    expect(source.now()).toBeGreaterThanOrEqual(0);
  });

  it("advances monotonically", () => {
    const { source, socket } = connect();
    let last = -Infinity;
    for (let i = 0; i < 40; i++) {
      socket.deliver(packBuckets([BUCKET], i * 100));
      source.tick(i * 16);
      expect(source.now()).toBeGreaterThanOrEqual(last);
      last = source.now();
    }
  });

  it("draws a stretch it never received as loss rather than joining across it", () => {
    const { source, socket } = connect();
    socket.deliver(packBuckets([BUCKET], 0));
    // A batch that starts 500 samples later: five buckets nobody has.
    socket.deliver(packBuckets([BUCKET], 600));
    const ring = source.decimator.rings[0]!;
    let gapped = 0;
    for (let k = 0; k < ring.count; k++) if (ring.gap[ring.idx(k)]! > 0) gapped++;
    expect(gapped).toBe(5);
  });

  it("takes the counters from the server rather than accumulating its own", () => {
    // The server knows about samples this console never received; accumulating
    // from what arrived would report a clean session over a hole.
    const { source, socket } = connect();
    socket.deliver(packBuckets([BUCKET], 0));
    socket.deliver(
      JSON.stringify({ type: "counters", total_stored: 900, total_missing: 100, gap_count: 1 }),
    );
    expect(source.decimator.totalStored).toBe(900);
    expect(source.decimator.totalMissing).toBe(100);
    expect(source.decimator.gapCount).toBe(1);
    // Idempotent: a repeat cannot inflate them.
    socket.deliver(
      JSON.stringify({ type: "counters", total_stored: 900, total_missing: 100, gap_count: 1 }),
    );
    expect(source.decimator.totalStored).toBe(900);
  });

  it("keeps what this browser missed apart from what the instrument lost", () => {
    const { source, socket } = connect();
    socket.deliver(packBuckets([BUCKET], 0));
    socket.deliver(
      JSON.stringify({ type: "desync", from_index: 100, to_index: 400, buckets_dropped: 3 }),
    );
    expect(source.displayGap).toBe(300);
    expect(source.decimator.gapCount).toBe(0);
  });

  it("replaces the distribution grid rather than adding to it", () => {
    const { source, socket } = connect();
    const bins = new Array(169).fill(0);
    bins[3] = 7;
    socket.deliver(packHistogram(bins));
    expect(source.decimator.histogram[3]).toBe(7);
    socket.deliver(packHistogram(bins));
    expect(source.decimator.histogram[3]).toBe(7);
  });

  it("clamps a ceiling downwards and never upwards", () => {
    // A page that could raise its own ceiling would not have one.
    const { source } = connect();
    source.setMaxVoltageMv(5000);
    expect(source.snapshot().maxVoltageMv).toBe(3600);
    source.setMaxVoltageMv(3000);
    expect(source.snapshot().maxVoltageMv).toBe(3000);
  });

  it("says the link is down without saying the device stopped", () => {
    const { source, socket } = connect();
    expect(source.snapshot().connection.phase).toBe("open");
    socket.drop();
    const connection = source.snapshot().connection;
    expect(connection.phase).toBe("reconnecting");
    // The device is still measuring as far as anyone knows. Only the view of
    // it stopped, and the two are reported separately.
    expect(source.snapshot().state.measuring).toBe(true);
    expect(connection.frozenAt).not.toBeNull();
  });
});
