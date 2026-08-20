/**
 * Types mirroring the machine-readable contracts in `ppk2lab`.
 *
 * Every name here has a counterpart in the Python package; when one moves, the
 * other has to move with it. `docs/api-baseline.md` freezes that surface and
 * `docs/SPEC.md` carries the stability policy.
 */

/** `ppk2lab.Mode` — wire values match the 0x11 command payload. */
export const Mode = { AMPERE: 1, SOURCE: 2 } as const;
export type Mode = (typeof Mode)[keyof typeof Mode];

/**
 * `ppk2lab.types.VoltageBasis` — where a voltage value came from.
 *
 * The PPK2 never measures the DUT's terminal voltage, so any energy figure is
 * derived from an assumption. This records which one, so a result can say how
 * much the number is worth.
 */
export type VoltageBasis =
  | "caller_override"
  | "configured_source"
  | "device_metadata"
  | "unknown";

/** `ppk2lab.DeviceState`. `null` means unknown / not observable, never a default. */
export interface DeviceState {
  mode: Mode | null;
  source_voltage_mv: number | null;
  /** Never readable back from the device; only ever what the host commanded. */
  dut_power: boolean | null;
  measuring: boolean;
  source_voltage_basis: VoltageBasis;
}

/**
 * `ppk2lab.StateChange` — one state-changing operation.
 *
 * `observed_after` separates values read back from the device from values the
 * host merely requested. A requested state is not a confirmed hardware state.
 */
export interface StateChange {
  operation: string;
  requested: Record<string, unknown>;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  applied: boolean;
  observed_after: boolean;
  warnings: string[];
}

/**
 * `ppk2lab.GapEvent` — a detected loss of samples in the 100 kS/s stream.
 *
 * `missing` is `null` when the true count is unknown (a host-side queue
 * overflow); `ambiguous` is true when the count came from the 6-bit counter and
 * could be larger by a multiple of 64.
 */
export interface GapEvent {
  index: number;
  missing: number | null;
  reason: string;
  ambiguous: boolean;
}

/** One entry of the `warnings` array carried by every JSON envelope. */
export interface Diagnostic {
  code: string;
  message: string;
  category: "capture integrity" | "measurement trust" | "device state" | "analysis" | string;
}

/** `ppk2lab.DeviceInfo`. */
export interface DeviceInfo {
  serial_number: string | null;
  vid: number | null;
  pid: number | null;
  firmware_version: string | null;
  simulated: boolean;
  measurement_port: string | null;
  /** `HW=… IA=… keys=… ports=…`, as reported by `PPK2.firmware_fingerprint()`. */
  fingerprint: string | null;
}

/** Per-range calibration constants parsed from the metadata reply. */
export interface RangeCalibration {
  r: number;
  gs: number;
  gi: number;
  o: number;
  s: number;
  i: number;
  ug: number;
}

export interface Calibration {
  calibrated: boolean | null;
  hw: number | null;
  ia: number | null;
  vdd_mv: number | null;
  ranges: RangeCalibration[];
  missing_ranges: number[];
  terminated: boolean;
}
