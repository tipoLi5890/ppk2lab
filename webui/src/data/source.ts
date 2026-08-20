import type { Decimator } from "../core/decimator";
import type { MessageKey } from "../i18n";
import type { Calibration, DeviceInfo, DeviceState, Mode, StateChange } from "../types";

/**
 * What the console reads from, and what it writes to.
 *
 * Two implementations satisfy it: `SimulatedSource`, which runs the shipped
 * `--simulate` profile in the browser, and `WebSocketSource`, which talks to the
 * Python supervisor that owns the one open `PPK2` session. Nothing in the
 * component tree knows which it has, so the console is testable without
 * hardware and identical with it.
 *
 * Sample data deliberately does **not** flow through React. At 100 kS/s a
 * `setState` per block would re-render the tree thousands of times a second, so
 * the chart reads `decimator` directly from its own draw loop and React is told
 * only about the things that change at human speed: device state, events, and
 * the outcome of a control operation.
 */

export type EventKind = "info" | "state" | "warn" | "loss";

/** A rendered-at-display-time log entry: it stores keys, not sentences. */
export interface ConsoleEvent {
  id: number;
  kind: EventKind;
  /** Operation name — a code identifier such as `set_mode`; never translated. */
  operation: string;
  messageKey: MessageKey;
  args: (string | number)[];
  /** before → after pairs, shown as a small table under the message. */
  delta?: Record<string, { from: string; to: string } | string>;
  /** Session-relative seconds; negative for anything before the page opened. */
  t: number;
  /**
   * For a state change: whether the resulting state was read back from the
   * device. Kept as a flag rather than a rendered sentence so switching
   * language re-renders the log rather than leaving it in the old one.
   */
  observed?: boolean;
}

export interface DeviceSnapshot {
  info: DeviceInfo;
  state: DeviceState;
  calibration: Calibration;
  /** Set only through an explicit assumption, never inferred. */
  assumedVoltageMv: number | null;
  /** Session ceiling protecting the DUT (`--max-voltage-mv`). */
  maxVoltageMv: number | null;
}

/** One requested state change, before it is known whether it can be applied. */
export type ControlRequest =
  | { kind: "mode"; mode: Mode }
  | { kind: "voltage"; voltageMv: number }
  | { kind: "dut-power"; on: boolean };

/**
 * The part of device state a user edits before committing it.
 *
 * DUT power is deliberately not in here. It is the output, not a setting: it
 * takes effect the moment it is sent, so it is never staged.
 */
export interface DeviceConfig {
  mode: Mode;
  voltageMv: number;
}

/** One staged difference, ready to be shown before anything is sent. */
export interface ConfigDiff {
  field: "mode" | "source_voltage_mv" | "dut_power";
  from: string;
  to: string;
}

/**
 * A staged configuration change and what has to happen around it.
 *
 * Two device facts shape every plan:
 *
 * - Mode and source voltage are refused while the device is measuring
 *   (`device.py`), so applying them breaks the stream.
 * - Changing the source voltage while VOUT is energised changes what the DUT
 *   receives, live. So a live output is dropped **before** the change lands,
 *   never during it, and only comes back if the operator asks for it.
 */
export interface ApplyPlan {
  changes: ControlRequest[];
  diff: ConfigDiff[];
  /** VOUT is live and will be de-energised before the changes are sent. */
  stopOutputFirst: boolean;
  /** Energise VOUT again once the changes are in. */
  restartOutput: boolean;
  /** The device refuses these changes while measuring. */
  interruptsStream: boolean;
}

/** One line of the sequence the dialog shows before the operator commits. */
export interface PlanStep {
  key: MessageKey;
  args: (string | number)[];
}

/**
 * The sequence a plan will run, in order, for the operator to read before
 * committing. Order is a safety property, not a presentation choice: the output
 * comes down before the settings move, and only goes back up at the end.
 */
export function planSteps(plan: ApplyPlan): PlanStep[] {
  const steps: PlanStep[] = [];
  if (plan.stopOutputFirst) steps.push({ key: "ap_step_stop_out", args: [] });
  if (plan.interruptsStream) steps.push({ key: "ap_step_stop_meas", args: [] });
  for (const d of plan.diff) steps.push({ key: "ap_step_apply", args: [d.field, d.to] });
  if (plan.interruptsStream) steps.push({ key: "ap_step_start_meas", args: [] });
  if (plan.restartOutput) steps.push({ key: "ap_step_start_out", args: [] });
  return steps;
}

export class ControlRejected extends Error {
  readonly code: string;
  readonly args: (string | number)[];
  constructor(code: string, args: (string | number)[] = []) {
    super(code);
    this.name = "ControlRejected";
    this.code = code;
    this.args = args;
  }
}

/** Extra affordances that only make sense against a simulated device. */
export interface DemoControls {
  setProfile(profile: "demo" | "idle"): void;
  profile(): "demo" | "idle";
  injectLoss(samples: number): void;
}

export interface DataSource {
  readonly simulated: boolean;
  /** Bucket rings the chart reads directly, outside React's render cycle. */
  readonly decimator: Decimator;

  /** Right edge of the chart: session-relative seconds. */
  now(): number;
  /** Oldest point any tier still retains, session-relative seconds. */
  historyStart(): number;

  snapshot(): DeviceSnapshot;
  events(): readonly ConsoleEvent[];

  /** Fires when the snapshot or the event list changes — not per sample. */
  subscribe(listener: () => void): () => void;

  isStreaming(): boolean;
  startStream(): void;
  stopStream(): void;

  /** Advance a simulated clock. A push-driven source ignores it. */
  tick(nowMs: number): void;

  /**
   * Dry run: report the change without sending a byte. Mirrors
   * `set_*(dry_run=True)`, which returns `applied: false` and `W_DRY_RUN`.
   */
  preview(request: ControlRequest): Promise<StateChange>;
  /** Apply a change that was previewed. Never called without a preview first. */
  apply(request: ControlRequest): Promise<StateChange>;
  /**
   * Run a staged plan in order: drop the output, stop measuring, apply, restore.
   * Returns one StateChange per step actually performed.
   */
  applyPlan(plan: ApplyPlan): Promise<StateChange[]>;

  /** Configuration as currently applied — what a draft is compared against. */
  config(): DeviceConfig;
  /** True while VOUT is energised, as far as the host requested. Never confirmed. */
  outputOn(): boolean;

  setAssumedVoltageMv(mv: number | null): void;
  setMaxVoltageMv(mv: number | null): void;

  readonly demo?: DemoControls;
}
