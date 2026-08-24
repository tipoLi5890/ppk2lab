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

/**
 * How this console's link to the server stands.
 *
 * Deliberately on the snapshot rather than on `DeviceState`: that type mirrors
 * `ppk2lab.DeviceState` field for field and has to keep doing so. This is a
 * fact about the console, not about the instrument -- and the two are not the
 * same fact. `measuring: true` with `phase: "reconnecting"` reads as "the
 * device is still running and this console can no longer see it", which is
 * exactly what an operator needs to be told rather than a frozen trace that
 * looks like a stopped measurement.
 */
export type ConnectionPhase = "simulated" | "connecting" | "open" | "reconnecting" | "closed";

export interface ConnectionStatus {
  phase: ConnectionPhase;
  /** Attempt number in the current backoff run; 0 while open. */
  attempt: number;
  /** Timestamp of the next attempt, or null. Not a countdown -- see useTicker. */
  nextAttemptAtMs: number | null;
  /** Session seconds of the newest data received. Where the trace froze. */
  frozenAt: number | null;
  /** Why it is not open, as a message key. Never a raw sentence. */
  reasonKey: MessageKey | null;
}

/**
 * What the server's sample pipeline is doing, as the server reports it.
 *
 * Deliberately not folded into `state.measuring`. That field is the *device's*
 * claim, carried by a `state` message; this is what the server is doing with
 * the result, carried by a `stream` message. They disagree honestly -- a plan
 * mid-flight leaves a measuring device behind a pipeline that is `applying` --
 * and writing either one from the other would hide whichever the operator
 * needed.
 */
export type StreamPhase = "running" | "stopped" | "applying" | "recording" | "stalled";

export interface StreamStatus {
  running: boolean;
  phase: StreamPhase;
  /** Why, when the server said so. A code, never a rendered sentence. */
  reason: string | null;
}

export const STREAM_PHASES: readonly StreamPhase[] = [
  "running",
  "stopped",
  "applying",
  "recording",
  "stalled",
];

export const CONNECTED: ConnectionStatus = {
  phase: "simulated",
  attempt: 0,
  nextAttemptAtMs: null,
  frozenAt: null,
  reasonKey: null,
};

/** What the device says a plan would do, before any of it is done. */
export interface PlanPreview {
  /** One per step, in order. Every one has `applied: false`. */
  steps: StateChange[];
  /** The server's answer, not the client's guess. */
  interruptsStream: boolean;
  /** Echoed back with applyPlan, so a stale preview cannot be applied. */
  stateSeq: number | null;
}

export interface DeviceSnapshot {
  info: DeviceInfo;
  state: DeviceState;
  /** The server's pipeline, which is not the same fact as `state.measuring`. */
  stream: StreamStatus;
  calibration: Calibration;
  /** Set only through an explicit assumption, never inferred. */
  assumedVoltageMv: number | null;
  /** Session ceiling protecting the DUT (`--max-voltage-mv`). */
  maxVoltageMv: number | null;
  connection: ConnectionStatus;
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
  /** What the device projected for this plan, when it was asked. */
  preview?: PlanPreview;
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

  /**
   * Start or stop measuring. Async because a refusal has to have somewhere to
   * go: returning void meant a device that said no said it to nobody.
   */
  startStream(): Promise<void>;
  stopStream(): Promise<void>;

  /** Advance a simulated clock. A push-driven source ignores it. */
  tick(nowMs: number): void;

  /**
   * Dry run of a whole plan: report what it would do without sending a byte.
   * Mirrors `set_*(dry_run=True)`, which returns `applied: false` and
   * `W_DRY_RUN`.
   *
   * A plan rather than a single request, because the *order* is the safety
   * property -- dropping the output before the voltage moves is the difference
   * between a controlled change and a live one -- and two independent
   * previews would each project an `after` that ignored the other.
   */
  previewPlan(plan: ApplyPlan): Promise<PlanPreview>;
  /**
   * Apply one change directly. Used only for the fail-safe direction, turning
   * the output off, which must never sit behind a confirmation.
   */
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

  /**
   * Reconnect now instead of waiting out the backoff. Present only on a source
   * that has a link to lose -- the simulated console never does, which is why
   * the button that calls it is drawn only when one is not open.
   */
  retryNow?(): void;

  readonly demo?: DemoControls;
  /**
   * Present only when something can actually write an artifact -- which means
   * a server. The panel shows what it can do rather than a button that cannot
   * work, the same way the demo controls vanish on a real device.
   */
  readonly recorder?: Recorder;
}

export interface RecordOptions {
  durationS?: number;
  sampleLimit?: number;
  output?: string;
  tags?: Record<string, string>;
  assumeVoltageMv?: number;
}

export interface RecordResult {
  path: string | null;
  complete: boolean;
  stored_samples: number;
  gap_count: number;
  capture_sha256: string | null;
  warnings: Array<{ code: string; message: string }>;
}

export interface Recorder {
  /**
   * Write a capture artifact and resolve when it is finished.
   *
   * It cannot be cancelled -- `run_capture` has no cooperative stop -- so the
   * options must bound it, and the console says so before it starts.
   */
  start(options: RecordOptions): Promise<RecordResult>;
}
