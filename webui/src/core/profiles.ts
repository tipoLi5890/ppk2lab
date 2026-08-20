import { CYCLE_SAMPLES, SAMPLE_RATE_HZ } from "./constants";

/**
 * The simulated profiles from `ppk2lab.testing`, reproduced client-side.
 *
 * These exist so the console runs end to end with no hardware, exactly as
 * `--simulate` does for the CLI. Simulated numbers are never a measurement, and
 * the UI says so on every screen.
 */

/** Deterministic PRNG. The jitter has to repeat across reloads to be comparable. */
export function mulberry32(seed: number): () => number {
  let a = seed;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface CycleData {
  /** Current per sample, microamps. */
  current: Float32Array;
  /** D0-D7 packed into one byte per sample. */
  logic: Uint8Array;
}

/** 8N1 UART line levels for `bytes`, one entry per sample. */
export function uartWave(bytes: readonly number[], baud: number, sampleRate = SAMPLE_RATE_HZ): number[] {
  const samplesPerBit = sampleRate / baud;
  const bits: number[] = [];
  for (const b of bytes) {
    bits.push(0); // start
    for (let i = 0; i < 8; i++) bits.push((b >> i) & 1); // LSB first
    bits.push(1); // stop
  }
  const out: number[] = [];
  for (let i = 0; i < bits.length; i++) {
    // Round bit boundaries against the grid rather than each bit's length, so
    // the accumulated error stays bounded instead of drifting per byte.
    const n = Math.round((i + 1) * samplesPerBit) - Math.round(i * samplesPerBit);
    for (let k = 0; k < n; k++) out.push(bits[i]!);
  }
  return out;
}

export interface SpiWave {
  sclk: number[];
  mosi: number[];
  miso: number[];
  cs: number[];
}

/** SPI mode-0 transaction: CS low, then one clock pulse per bit, MSB first. */
export function spiWave(
  mosiBytes: readonly number[],
  misoBytes: readonly number[],
  clockHz: number,
  sampleRate = SAMPLE_RATE_HZ,
): SpiWave {
  const half = Math.round(sampleRate / clockHz / 2);
  const w: SpiWave = { sclk: [], mosi: [], miso: [], cs: [] };
  const push = (n: number, c: number, mo: number, mi: number, s: number) => {
    for (let k = 0; k < n; k++) {
      w.sclk.push(c);
      w.mosi.push(mo);
      w.miso.push(mi);
      w.cs.push(s);
    }
  };
  push(half, 0, 0, 0, 1); // idle, CS high
  push(half, 0, 0, 0, 0); // CS asserted
  for (let bi = 0; bi < mosiBytes.length; bi++) {
    for (let i = 7; i >= 0; i--) {
      const mo = (mosiBytes[bi]! >> i) & 1;
      const mi = ((misoBytes[bi] ?? 0) >> i) & 1;
      push(half, 0, mo, mi, 0);
      push(half, 1, mo, mi, 0);
    }
  }
  push(half, 0, 0, 0, 0);
  push(half, 0, 0, 0, 1);
  return w;
}

/**
 * `profiles.py DemoActivityProfile` — the repeating 100 ms activity cycle.
 *
 * Per cycle: samples 0-1499 an active burst at 12 mA with D1 as a busy marker
 * and one 5 kHz SPI transaction on D3-D6; then `TX_DONE\n` at 9600 baud on D0
 * while drawing 25 µA; then 6 µA sleep. D2 carries a 1 kHz square throughout.
 */
export function buildDemoCycle(): CycleData {
  const SLEEP_UA = 6.0;
  const UART_UA = 25.0;
  const BURST_UA = 12_000.0;

  const current = new Float32Array(CYCLE_SAMPLES);
  const logic = new Uint8Array(CYCLE_SAMPLES);
  const d: Uint8Array[] = Array.from({ length: 8 }, () => new Uint8Array(CYCLE_SAMPLES));

  d[0]!.fill(1); // UART idles high
  d[6]!.fill(1); // CS idles high

  current.fill(SLEEP_UA);
  for (let i = 0; i < 1500; i++) {
    current[i] = BURST_UA;
    d[1]![i] = 1;
  }

  const uart = uartWave([0x54, 0x58, 0x5f, 0x44, 0x4f, 0x4e, 0x45, 0x0a], 9600); // "TX_DONE\n"
  for (let i = 0; i < uart.length && 1500 + i < CYCLE_SAMPLES; i++) {
    d[0]![1500 + i] = uart[i]!;
    current[1500 + i] = UART_UA;
  }

  for (let i = 0; i < CYCLE_SAMPLES; i++) d[2]![i] = Math.floor(i / 50) % 2; // 1 kHz

  const spi = spiWave([0x9f, 0x00], [0x00, 0x52], 5000);
  const offset = 200;
  for (let i = 0; i < spi.sclk.length && offset + i < CYCLE_SAMPLES; i++) {
    d[3]![offset + i] = spi.sclk[i]!;
    d[4]![offset + i] = spi.mosi[i]!;
    d[5]![offset + i] = spi.miso[i]!;
    d[6]![offset + i] = spi.cs[i]!;
  }

  for (let i = 0; i < CYCLE_SAMPLES; i++) {
    let b = 0;
    for (let c = 0; c < 8; c++) if (d[c]![i]) b |= 1 << c;
    logic[i] = b;
  }
  return { current, logic };
}

/**
 * An unloaded meter: the measured noise floor.
 *
 * One unit with nothing on VOUT read mean 0.1633 µA with a minimum of
 * −0.2477 µA over 60 s. Negative samples are routine there — the conversion
 * subtracts a per-range offset, so residual noise near true zero falls on both
 * sides of it. This profile exists to exercise that path: the console must show
 * negatives, and must show quantiles pinned at the 200 nA floor as bounds.
 */
export function buildIdleCycle(seed = 0x1234): CycleData {
  const current = new Float32Array(CYCLE_SAMPLES);
  const logic = new Uint8Array(CYCLE_SAMPLES);
  const rnd = mulberry32(seed);
  for (let i = 0; i < CYCLE_SAMPLES; i++) {
    const g = rnd() + rnd() + rnd() + rnd() - 2; // approximately normal
    current[i] = 0.1633 + g * 0.75 * 0.22;
  }
  return { current, logic };
}
