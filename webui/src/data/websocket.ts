import { SAMPLE_RATE_HZ } from "../core/constants";
import { Decimator } from "../core/decimator";
import type { MessageKey } from "../i18n";
import { Mode, type Calibration, type DeviceInfo, type DeviceState, type StateChange } from "../types";
import { errorMessage, warningMessage } from "./codes";
import { Link, LinkClosed, type LinkOptions, type Phase } from "./link";
import {
  PROTOCOL_VERSION,
  decodeFrame,
  eachBucket,
  missingBucket,
  type BucketBatch,
} from "./protocol";
import {
  ControlRejected,
  type ApplyPlan,
  type ConnectionStatus,
  type ConsoleEvent,
  type ControlRequest,
  type DataSource,
  type DeviceConfig,
  type DeviceSnapshot,
  type PlanPreview,
} from "./source";

/**
 * A `DataSource` backed by a `ppk2lab web` server.
 *
 * The two halves of the console meet here. Samples never pass through React:
 * bucket frames are decoded and pushed straight into the `Decimator` the chart
 * reads from its own animation loop. Everything a person acts on -- state, the
 * session log, connection status -- goes through `notify()` at human speed.
 *
 * Three rules hold this together, and each of them has a failure mode that is
 * silent if it is broken:
 *
 * 1. `snapshot()` and `events()` return cached objects. `useSyncExternalStore`
 *    compares by identity on every render, so building a fresh one per call is
 *    an infinite render loop.
 * 2. `config()`, `outputOn()` and `simulated` are read *during* render and are
 *    not part of the store. They ride the snapshot's identity change, so state
 *    is mutated **before** listeners fire, never after.
 * 3. Only `ingestBucket` and `breakContinuity` touch the decimator.
 *    `pushSample`/`pushGap` drive a separate accumulator with its own clock;
 *    mixing the two emits buckets at fabricated times.
 */

/** The right edge sits just behind the newest data, so it never overruns it. */
const TARGET_LAG_S = 0.12;
/** Past this the clock jumps rather than chases -- a tab that was hidden. */
const SNAP_S = 1.5;
/** Rate skew while catching up. Below the threshold of visibility on a scroll. */
const MAX_RATE_SKEW = 0.1;

/** A hole longer than this is a new session rather than something to fill in. */
const MAX_FILL_BUCKETS = 600_000;

const TIMEOUT_FAST_MS = 5_000;
/** A plan stops a stream, writes several commands and reopens one. */
const TIMEOUT_PLAN_MS = 20_000;

const UNKNOWN_STATE: DeviceState = {
  mode: null,
  source_voltage_mv: null,
  dut_power: null,
  measuring: false,
  source_voltage_basis: "unknown",
};

const UNKNOWN_INFO: DeviceInfo = {
  serial_number: null,
  vid: null,
  pid: null,
  firmware_version: null,
  simulated: false,
  measurement_port: null,
  fingerprint: null,
};

const UNKNOWN_CALIBRATION: Calibration = {
  calibrated: null,
  hw: null,
  ia: null,
  vdd_mv: null,
  ranges: [],
  missing_ranges: [],
  terminated: false,
};

export interface WebSocketSourceOptions extends LinkOptions {
  /** Overridden in tests; the browser's own clock otherwise. */
  clock?: () => number;
}

export class WebSocketSource implements DataSource {
  readonly decimator = new Decimator();

  private readonly link: Link;
  private readonly listeners = new Set<() => void>();

  private info = UNKNOWN_INFO;
  private state = UNKNOWN_STATE;
  private calibration = UNKNOWN_CALIBRATION;
  private applied: DeviceConfig = { mode: Mode.SOURCE, voltageMv: 3000 };
  private assumedVoltageMv: number | null = null;
  /** The server's ceiling. A console may lower it for itself, never raise it. */
  private sessionCeiling: number | null = null;
  private maxVoltageMv: number | null = null;
  private controlToken: string | null = null;
  private controlAllowed = false;
  private stateSeq: number | null = null;
  private helloSeen = false;

  private connection: ConnectionStatus = {
    phase: "connecting",
    attempt: 0,
    nextAttemptAtMs: null,
    frozenAt: null,
    reasonKey: null,
  };

  private eventLog: ConsoleEvent[] = [];
  private eventsCache: readonly ConsoleEvent[] = [];
  private snapshotCache: DeviceSnapshot | null = null;
  private nextEventId = 1;

  /** Timeline index this console attached at. t = 0 is that moment. */
  private attachIndex = 0;
  /** Where the next bucket should start; -1 until the first batch. */
  private nextIndex = -1;
  private edge = 0;
  private displayed = 0;
  private lastTickMs: number | null = null;

  /** Samples this browser never received. Kept apart from instrument loss. */
  displayGap = 0;

  constructor(options: WebSocketSourceOptions) {
    this.link = new Link(options, {
      onJson: (message) => this.onJson(message),
      onBinary: (frame) => this.onBinary(frame),
      onPhase: (phase, info) => this.onPhase(phase, info),
    });
    this.link.start();
  }

  close(): void {
    this.link.close();
  }

  // -- identity ---------------------------------------------------------
  get simulated(): boolean {
    return this.info.simulated;
  }

  // -- the chart's clock ------------------------------------------------
  now(): number {
    return this.displayed;
  }

  historyStart(): number {
    let oldest = Infinity;
    for (const ring of this.decimator.rings) {
      const t = ring.oldest();
      if (t < oldest) oldest = t;
    }
    // Never Infinity: the chart does arithmetic on this every frame.
    return Number.isFinite(oldest) ? oldest : 0;
  }

  /**
   * Advance the right edge smoothly between batches.
   *
   * Buckets arrive every 50-100 ms and the chart redraws at 30 fps, so a clock
   * that only moved when data landed would stair-step visibly. Between batches
   * it free-runs at wall rate; a batch nudges the rate by at most ten percent.
   *
   * The clamp to `edge` is the load-bearing part. Drawing past the newest data
   * leaves a blank strip at the right: nothing fills it, so it renders as "the
   * DUT drew nothing" -- a measurement that was never taken.
   */
  tick(nowMs: number): void {
    if (this.lastTickMs === null) {
      this.lastTickMs = nowMs;
      return;
    }
    const dt = Math.min((nowMs - this.lastTickMs) / 1000, 0.25);
    this.lastTickMs = nowMs;
    if (this.connection.phase !== "open" || !this.state.measuring) return;

    const target = this.edge - TARGET_LAG_S;
    const error = target - this.displayed;
    if (Math.abs(error) > SNAP_S) {
      // A hidden tab stops requestAnimationFrame while the socket keeps
      // delivering, so the clock is far behind on resume and jumps.
      this.displayed = target;
      return;
    }
    const skew = Math.max(-MAX_RATE_SKEW, Math.min(MAX_RATE_SKEW, error * 2));
    this.displayed += dt * (1 + skew);
    if (this.displayed > this.edge) this.displayed = this.edge;
  }

  // -- the store --------------------------------------------------------
  snapshot(): DeviceSnapshot {
    if (!this.snapshotCache) {
      this.snapshotCache = {
        info: this.info,
        state: this.state,
        calibration: this.calibration,
        assumedVoltageMv: this.assumedVoltageMv,
        maxVoltageMv: this.maxVoltageMv,
        connection: this.connection,
      };
    }
    return this.snapshotCache;
  }

  events(): readonly ConsoleEvent[] {
    return this.eventsCache;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private notify(): void {
    this.snapshotCache = null;
    this.eventsCache = this.eventLog.slice();
    for (const listener of this.listeners) listener();
  }

  config(): DeviceConfig {
    return this.applied;
  }

  outputOn(): boolean {
    return this.state.dut_power === true;
  }

  // -- session-local assumptions ----------------------------------------
  /**
   * The assumed voltage is the *caller's* assumption, and this browser is the
   * caller. It changes which voltage the energy arithmetic uses and nothing
   * about the device, so it never reaches the wire.
   */
  setAssumedVoltageMv(mv: number | null): void {
    this.assumedVoltageMv = mv;
    this.notify();
  }

  /**
   * Clamp-only. The session ceiling comes from `--max-voltage-mv` and exists to
   * protect the DUT; a page that could raise its own ceiling would not have
   * one. Lowering it for this console is useful and safe, and the server
   * re-validates every request regardless.
   */
  setMaxVoltageMv(mv: number | null): void {
    const ceiling = this.sessionCeiling;
    this.maxVoltageMv =
      mv === null ? ceiling : ceiling === null ? mv : Math.min(mv, ceiling);
    this.notify();
  }

  // -- control ----------------------------------------------------------
  async startStream(): Promise<void> {
    await this.command("start_stream", {}, TIMEOUT_FAST_MS);
  }

  async stopStream(): Promise<void> {
    await this.command("stop_stream", {}, TIMEOUT_FAST_MS);
  }

  async previewPlan(plan: ApplyPlan): Promise<PlanPreview> {
    const result = (await this.command(
      "preview_plan",
      { plan: planJson(plan) },
      TIMEOUT_FAST_MS,
    )) as {
      steps: StateChange[];
      interruptsStream: boolean;
      stateSeq: number;
    };
    return {
      steps: result.steps,
      interruptsStream: result.interruptsStream,
      stateSeq: result.stateSeq,
    };
  }

  async apply(request: ControlRequest): Promise<StateChange> {
    return (await this.command(
      "apply",
      { request: requestJson(request) },
      TIMEOUT_FAST_MS,
    )) as StateChange;
  }

  async applyPlan(plan: ApplyPlan): Promise<StateChange[]> {
    const result = (await this.command(
      "apply_plan",
      { plan: planJson(plan), stateSeq: plan.preview?.stateSeq ?? null },
      TIMEOUT_PLAN_MS,
    )) as { steps: StateChange[] };
    return result.steps;
  }

  async record(options: Record<string, unknown>): Promise<Record<string, unknown>> {
    // A recording writes an artifact and cannot be interrupted, so it is bound
    // by a duration the operator chose and waited for in full.
    return (await this.command("record", { options }, 0)) as Record<string, unknown>;
  }

  private async command(
    op: string,
    fields: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<unknown> {
    if (!this.controlAllowed && op !== "preview_plan") {
      throw new ControlRejected("er_locked");
    }
    try {
      const reply = await this.link.request(
        op,
        { ...fields, token: this.controlToken },
        timeoutMs || 24 * 60 * 60 * 1000,
      );
      if (reply.ok === true) return reply.result;
      const error = (reply.error ?? {}) as { messageKey?: string; args?: (string | number)[] };
      throw new ControlRejected(error.messageKey ?? "er_unknown", error.args ?? []);
    } catch (err) {
      if (err instanceof ControlRejected) throw err;
      if (err instanceof LinkClosed) throw new ControlRejected(err.reasonKey, [String(timeoutMs / 1000)]);
      throw new ControlRejected("er_unknown", ["UNEXPECTED", String(err)]);
    }
  }

  // -- incoming ---------------------------------------------------------
  private onPhase(phase: Phase, info: { attempt: number; nextAttemptAtMs: number | null; reasonKey: string | null }): void {
    const was = this.connection.phase;
    this.connection = {
      phase,
      attempt: info.attempt,
      nextAttemptAtMs: info.nextAttemptAtMs,
      frozenAt: phase === "open" ? null : this.edge,
      reasonKey: (info.reasonKey as MessageKey | null) ?? null,
    };
    if (phase !== "open" && was === "open") {
      // The trace must break here. Joining across a stretch this console never
      // received would draw a line through data nobody has.
      this.decimator.breakContinuity();
      this.log("warn", "connection", "ev_conn_lost", []);
    }
    this.notify();
  }

  private onJson(message: Record<string, unknown>): void {
    switch (message.type) {
      case "hello":
        this.onHello(message);
        return;
      case "state":
        this.onState(message);
        return;
      case "counters":
        this.onCounters(message);
        return;
      case "event":
        this.onEvent(message);
        return;
      case "gap":
        this.onGap(message);
        return;
      case "stream":
        this.notify();
        return;
      case "desync":
        this.onDesync(message);
        return;
      case "error":
        this.onError(message);
        return;
      default:
        // An unknown message type is unknown, not invalid. The server's
        // catalogues are published as open and this one is too.
        return;
    }
  }

  private onHello(message: Record<string, unknown>): void {
    const protocol = Number(message.protocol);
    if (protocol !== PROTOCOL_VERSION) {
      // Refused before a single binary frame arrives. The bundle ships inside
      // the wheel and nothing rebuilds it at install time, so an old console
      // meeting a new server is ordinary -- and a changed frame layout read as
      // numbers is a fabricated current trace.
      this.connection = {
        ...this.connection,
        reasonKey: "conn_version",
      };
      this.link.giveUp("conn_version");
      this.log("warn", "protocol", "conn_version", [protocol, PROTOCOL_VERSION]);
      this.notify();
      return;
    }
    const session = (message.session ?? {}) as Record<string, unknown>;
    const limits = (message.limits ?? {}) as Record<string, unknown>;

    this.info = (message.device as DeviceInfo) ?? UNKNOWN_INFO;
    this.state = stateFrom(message.state);
    this.calibration = (message.calibration as Calibration) ?? UNKNOWN_CALIBRATION;
    this.applied = configFrom(this.state);
    this.controlAllowed = session.control_allowed === true;
    this.controlToken = (session.control_token as string) ?? null;
    this.sessionCeiling = (limits.max_voltage_mv as number | null) ?? null;
    this.maxVoltageMv = this.sessionCeiling;
    this.stateSeq = (message.state_seq as number) ?? null;

    if (!this.helloSeen) {
      // Recorded once. A reconnection continues the same timeline, so t = 0
      // stays the moment this console attached -- otherwise every event in the
      // log would jump when the socket came back.
      this.attachIndex = Number(session.attach_index ?? 0);
      this.helloSeen = true;
    }
    this.link.markEstablished();
    this.log("info", "connect", "ev_conn_open", []);
    this.notify();
  }

  private onState(message: Record<string, unknown>): void {
    this.state = stateFrom(message.state);
    this.applied = configFrom(this.state);
    this.stateSeq = (message.state_seq as number) ?? this.stateSeq;
    const change = message.change as StateChange | null;
    if (change) {
      this.log("state", change.operation, "ev_applied", [], deltaOf(change), change.observed_after);
      for (const warning of change.warnings ?? []) {
        const { key, args } = warningMessage(warning, warning);
        this.log("warn", change.operation, key, args);
      }
    }
    this.notify();
  }

  private onCounters(message: Record<string, unknown>): void {
    // Assigned, never accumulated. The server knows about samples this console
    // never received, so accumulating from what arrived would report a clean
    // session over a hole -- and assignment repairs itself after any drop.
    this.decimator.totalStored = Number(message.total_stored ?? 0);
    this.decimator.totalMissing = Number(message.total_missing ?? 0);
    this.decimator.gapCount = Number(message.gap_count ?? 0);
  }

  private onEvent(message: Record<string, unknown>): void {
    this.log(
      (message.kind as ConsoleEvent["kind"]) ?? "info",
      String(message.operation ?? ""),
      (message.messageKey as MessageKey) ?? "w_unknown",
      (message.args as (string | number)[]) ?? [],
      message.delta as ConsoleEvent["delta"] | undefined,
      message.observed as boolean | undefined,
    );
    this.notify();
  }

  private onGap(message: Record<string, unknown>): void {
    const missing = message.missing as number | null;
    this.log("loss", "sample_gap", "ev_gap", [missing ?? 0, Number(message.index ?? 0)]);
    this.notify();
  }

  private onDesync(message: Record<string, unknown>): void {
    const from = Number(message.from_index ?? 0);
    const to = Number(message.to_index ?? 0);
    const missed = Math.max(0, to - from);
    this.displayGap += missed;
    // Kept apart from `gapCount` on purpose: "the instrument never delivered
    // it" and "this browser did not receive it" are different facts, and
    // drawing them the same way would let a slow link look like a lossy
    // instrument. The counters and the histogram are absolute, so no statistic
    // is affected -- only the chart, which is exactly the truth.
    this.fillHole(from, to);
    this.decimator.breakContinuity();
    this.log("loss", "desync", "ev_desync", [missed]);
    this.notify();
  }

  private onError(message: Record<string, unknown>): void {
    const { key, args } = errorMessage(
      String(message.code ?? "UNKNOWN"),
      (message.args as (string | number)[]) ?? [],
    );
    this.log("warn", String(message.code ?? "error"), key, args);
    this.notify();
  }

  private onBinary(buffer: ArrayBuffer): void {
    let frame;
    try {
      frame = decodeFrame(buffer);
    } catch {
      // A frame this build did not expect is not decoded into numbers. Stop
      // rather than draw something that was never measured.
      this.link.giveUp("conn_version");
      this.notify();
      return;
    }
    if (frame.kind === "histogram") {
      // Absolute, so a dropped frame repairs itself on the next one.
      this.decimator.histogram.set(frame.bins);
      return;
    }
    this.ingest(frame);
  }

  private ingest(batch: BucketBatch): void {
    if (this.nextIndex >= 0 && batch.firstIndex > this.nextIndex) {
      this.fillHole(this.nextIndex, batch.firstIndex);
    }
    if (batch.discontinuity) this.decimator.breakContinuity();
    const skipBefore = this.nextIndex;
    const end = eachBucket(batch, this.attachIndex, SAMPLE_RATE_HZ, (bucket, index) => {
      // A replayed backlog can overlap what this console already holds.
      if (skipBefore >= 0 && index < skipBefore) return;
      this.decimator.ingestBucket(bucket);
    });
    this.nextIndex = Math.max(this.nextIndex, end);
    this.edge = (this.nextIndex - this.attachIndex) / SAMPLE_RATE_HZ;
  }

  /**
   * Draw a stretch this console never received as loss.
   *
   * Without it `columns.collect` finds nothing there and joins straight across
   * -- a clean line over a span nobody has data for. Hatched instead, with the
   * mean line broken, which is what the same span gets when the instrument is
   * the one that lost it.
   */
  private fillHole(from: number, to: number): void {
    const samples = to - from;
    if (samples <= 0) return;
    const buckets = Math.ceil(samples / 100);
    if (buckets > MAX_FILL_BUCKETS) {
      // Longer than the ring retains. Filling it would churn through hundreds
      // of thousands of buckets to display nothing; start again instead.
      this.decimator.breakContinuity();
      this.nextIndex = to;
      this.log("warn", "resync", "ev_conn_resync", [samples]);
      return;
    }
    let index = from;
    while (index < to) {
      const width = Math.min(100, to - index);
      this.decimator.ingestBucket(missingBucket(index, this.attachIndex, SAMPLE_RATE_HZ, width));
      index += width;
    }
    this.decimator.breakContinuity();
    this.nextIndex = to;
  }

  private log(
    kind: ConsoleEvent["kind"],
    operation: string,
    messageKey: MessageKey,
    args: (string | number)[],
    delta?: ConsoleEvent["delta"],
    observed?: boolean,
  ): void {
    // Built conditionally: `exactOptionalPropertyTypes` is on, so an explicit
    // `undefined` is not the same as an absent field.
    const event: ConsoleEvent = {
      id: this.nextEventId++,
      kind,
      operation,
      messageKey,
      args,
      t: this.edge,
    };
    if (delta !== undefined) event.delta = delta;
    if (observed !== undefined) event.observed = observed;
    this.eventLog.push(event);
    if (this.eventLog.length > 400) this.eventLog.splice(0, this.eventLog.length - 400);
  }

  /** Reconnect without waiting out the backoff. */
  retryNow(): void {
    this.link.retryNow();
  }
}

/**
 * Adapt the library's own state to the console's.
 *
 * `DeviceState.to_json()` serialises the mode as `"source"` / `"ampere"`,
 * which is the frozen shape `ppk2lab info --json` publishes; the console
 * carries the numeric wire value the device command uses. Converting here
 * keeps the socket and the CLI structurally identical rather than inventing a
 * third spelling for one field -- and an unrecognised value stays `null`,
 * which the console shows as UNKNOWN rather than guessing.
 */
function stateFrom(raw: unknown): DeviceState {
  const state = (raw ?? {}) as Record<string, unknown>;
  const mode =
    state.mode === "source" ? Mode.SOURCE : state.mode === "ampere" ? Mode.AMPERE : null;
  return {
    mode,
    source_voltage_mv: (state.source_voltage_mv as number | null) ?? null,
    dut_power: (state.dut_power as boolean | null) ?? null,
    measuring: state.measuring === true,
    source_voltage_basis: (state.source_voltage_basis as DeviceState["source_voltage_basis"]) ??
      "unknown",
  };
}

function configFrom(state: DeviceState): DeviceConfig {
  return {
    mode: state.mode ?? Mode.SOURCE,
    voltageMv: state.source_voltage_mv ?? 0,
  };
}

function requestJson(request: ControlRequest): Record<string, unknown> {
  if (request.kind === "mode") return { kind: "mode", mode: request.mode };
  if (request.kind === "voltage") return { kind: "voltage", voltageMv: request.voltageMv };
  return { kind: "dut-power", on: request.on };
}

function planJson(plan: ApplyPlan): Record<string, unknown> {
  return {
    changes: plan.changes.map(requestJson),
    stopOutputFirst: plan.stopOutputFirst,
    restartOutput: plan.restartOutput,
  };
}

function deltaOf(change: StateChange): ConsoleEvent["delta"] {
  const delta: Record<string, { from: string; to: string }> = {};
  for (const key of Object.keys(change.before ?? {})) {
    delta[key] = {
      from: String((change.before ?? {})[key]),
      to: String((change.after ?? {})[key]),
    };
  }
  return delta;
}
