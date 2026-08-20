/** Constants taken from `ppk2lab`; each one names its source. */

/** `ppk2lab.SAMPLE_RATE_HZ` — fixed. Current and D0-D7 share one timeline. */
export const SAMPLE_RATE_HZ = 100_000;
/** One sample every 10 us. */
export const SAMPLE_PERIOD_S = 1 / SAMPLE_RATE_HZ;

/** `ppk2lab.VOLTAGE_MIN_MV` / `VOLTAGE_MAX_MV` — device capability. */
export const VOLTAGE_MIN_MV = 800;
export const VOLTAGE_MAX_MV = 5000;

/** `transport/mock.py FULL_SCALE_UA` — the five auto-switching shunt ranges. */
export const FULL_SCALE_UA = [50, 500, 5_000, 50_000, 1_000_000] as const;
export const RANGE_COUNT = FULL_SCALE_UA.length;

/** Simulated per-range resistor constants (`transport/mock.py`). */
export const SIM_CAL_R = [
  14399.12109375, 1439.912109375, 143.9912109375, 14.399121093749999, 0.7199560546875,
] as const;

/** `capture/stats.py QUANTILE_MIN_UA` — the distribution grid's 200 nA floor. */
export const QUANTILE_MIN_UA = 0.2;

/** Log-axis domain for the current plot, in microamps. */
export const AXIS_MIN_UA = 0.1;
export const AXIS_MAX_UA = 1_000_000;

/** `profiles.py DemoActivityProfile.CYCLE_SAMPLES` — one 100 ms activity cycle. */
export const CYCLE_SAMPLES = 10_000;

/** `ppk2lab.DIGITAL_CHANNELS`, bit order: bit N of the logic byte is D<N>. */
export const DIGITAL_CHANNELS = ["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"] as const;

/** What each channel carries in the shipped simulated profile. */
export const SIM_CHANNEL_ROLE = [
  "UART TX", "BUSY", "1 kHz", "SCLK", "MOSI", "MISO", "CS", "—",
] as const;

/**
 * Decimation tiers. Each tier is ten of the tier below it, so a bucket is built
 * once and folded upward rather than recomputed per tier.
 */
export interface Tier {
  /** Bucket width in milliseconds. */
  readonly ms: number;
  /** Ring capacity in buckets. */
  readonly cap: number;
  readonly label: string;
}
export const TIERS: readonly Tier[] = [
  { ms: 1, cap: 10_000, label: "1 ms" },      //  10 s
  { ms: 10, cap: 12_000, label: "10 ms" },    // 120 s
  { ms: 100, cap: 12_000, label: "100 ms" },  //  20 min
  { ms: 1000, cap: 7_200, label: "1 s" },     //   2 h
];

/** Samples per tier-0 bucket. Tier 0 is 1 ms at 100 kS/s. */
export const SAMPLES_PER_BUCKET = (SAMPLE_RATE_HZ * TIERS[0]!.ms) / 1000;

/** Decoder rate tiers (`decoders/feasibility.py`). Samples per bit/cycle. */
export const TIER_VALIDATED_MIN = 10;
export const TIER_CONDITIONAL_MIN = 5;
export const TIER_EXPERIMENTAL_MIN = 2.5;

/** Smallest range whose full scale covers `uA`. Mirrors `current_to_range_adc`. */
export function rangeFor(uA: number): number {
  const c = Math.max(uA, 0);
  for (let r = 0; r < RANGE_COUNT; r++) {
    if (c <= FULL_SCALE_UA[r] || r === RANGE_COUNT - 1) return r;
  }
  return RANGE_COUNT - 1;
}
