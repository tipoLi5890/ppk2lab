import {
  CYCLE_SAMPLES,
  SAMPLES_PER_BUCKET,
  SAMPLE_RATE_HZ,
  SIM_CAL_R,
  VOLTAGE_MAX_MV,
  VOLTAGE_MIN_MV,
  rangeFor,
} from "../core/constants";
import { Decimator, type Bucket } from "../core/decimator";
import { fmtInt } from "../core/format";
import { histAdd, newHistogram } from "../core/histogram";
import { buildDemoCycle, buildIdleCycle, mulberry32, type CycleData } from "../core/profiles";
import type { MessageKey } from "../i18n";
import { Mode, type Calibration, type DeviceState, type StateChange, type VoltageBasis } from "../types";
import {
  ControlRejected,
  type ApplyPlan,
  type ConsoleEvent,
  type ControlRequest,
  type DataSource,
  type DemoControls,
  type DeviceConfig,
  type DeviceSnapshot,
  type EventKind,
} from "./source";

/** Seconds of history synthesised before the page opened. */
const PREFILL_S = 600;

/** Losses planted in the pre-recorded history so gap rendering is visible on load. */
const PLANTED_GAPS: readonly [number, number][] = [
  [-472.3, 5120],
  [-311.0, 4864],
  [-158.9, 23104],
  [-47.6, 1180],
];

/**
 * The shipped `--simulate` device, running in the browser.
 *
 * History before the page opened is replayed at bucket level: the profile is
 * periodic, so one cycle's buckets are computed once and replayed with
 * cycle-to-cycle jitter. Everything from the moment the page opens is generated
 * sample by sample and pushed through the same decimator the real client uses,
 * which is what makes this a test of the decimation and not just of the chart.
 */
export class SimulatedSource implements DataSource {
  readonly simulated = true;
  readonly decimator = new Decimator();

  private readonly demoCycle: CycleData = buildDemoCycle();
  private readonly idleCycle: CycleData = buildIdleCycle();
  private activeProfile: "demo" | "idle" = "demo";

  private readonly rnd = mulberry32(0x9f52);
  private t = 0;
  private sampleIndex = 0;
  private pendingLoss = 0;
  private streaming = true;

  private mode: Mode = Mode.SOURCE;
  private voltageMv = 3000;
  private vddMv = 3000;
  private dutPower: boolean | null = false;
  private dutObserved = false;
  private assumedVoltageMv: number | null = null;
  private maxVoltageMv: number | null = 3600;

  private eventLog: ConsoleEvent[] = [];
  private nextEventId = 1;
  private listeners = new Set<() => void>();
  private snapshotCache: DeviceSnapshot | null = null;
  private eventsCache: readonly ConsoleEvent[] = [];

  constructor() {
    this.log("info", "open", "ev_open", ["simulated:SIM0001"], undefined, -PREFILL_S);
    this.log("info", "refresh_metadata", "ev_meta", [], undefined, -PREFILL_S + 0.4);
    this.log("state", "start_measuring", "ev_start", [], undefined, -PREFILL_S + 0.6);
    this.prefill();
  }

  // -- history ------------------------------------------------------------

  private cycle(): CycleData {
    return this.activeProfile === "demo" ? this.demoCycle : this.idleCycle;
  }

  /** One profile cycle reduced to its tier-0 buckets, computed once per replay. */
  private cycleBuckets(cyc: CycleData): Bucket[] {
    const out: Bucket[] = [];
    const perCycle = CYCLE_SAMPLES / SAMPLES_PER_BUCKET;
    for (let b = 0; b < perCycle; b++) {
      let min = Infinity;
      let max = -Infinity;
      let sum = 0;
      let rangeMask = 0;
      let logicAny = 0;
      let logicAll = 0xff;
      let edges = 0;
      let last = -1;
      for (let k = 0; k < SAMPLES_PER_BUCKET; k++) {
        const i = b * SAMPLES_PER_BUCKET + k;
        const v = cyc.current[i]!;
        const lg = cyc.logic[i]!;
        if (v < min) min = v;
        if (v > max) max = v;
        sum += v;
        rangeMask |= 1 << rangeFor(v);
        logicAny |= lg;
        logicAll &= lg;
        if (last >= 0) {
          let x = last ^ lg;
          while (x) {
            x &= x - 1;
            edges++;
          }
        }
        last = lg;
      }
      out.push({ t0: 0, min, max, sum, n: SAMPLES_PER_BUCKET, gap: 0, rangeMask, logicAny, logicAll, edges });
    }
    return out;
  }

  private prefill(): void {
    const cyc = this.cycle();
    const template = this.cycleBuckets(cyc);
    const cycleHist = newHistogram();
    for (let i = 0; i < CYCLE_SAMPLES; i++) histAdd(cycleHist, cyc.current[i]!);

    const cycleSeconds = CYCLE_SAMPLES / SAMPLE_RATE_HZ;
    const cycles = Math.round(PREFILL_S / cycleSeconds);
    const planted = new Map<number, number>();
    for (const [at, missing] of PLANTED_GAPS) {
      planted.set(Math.round((at + PREFILL_S) / cycleSeconds), missing);
    }

    let t = -PREFILL_S;
    const bucketSeconds = SAMPLES_PER_BUCKET / SAMPLE_RATE_HZ;
    for (let c = 0; c < cycles; c++) {
      const jitter = 1 + (this.rnd() - 0.5) * 0.05;
      const missing = planted.get(c) ?? 0;
      let left = missing;
      for (const a of template) {
        let gap = 0;
        let n = a.n;
        if (left > 0) {
          gap = Math.min(SAMPLES_PER_BUCKET, left);
          left -= gap;
          n = SAMPLES_PER_BUCKET - gap;
        }
        this.decimator.ingestBucket({
          t0: t,
          min: a.min * jitter,
          max: a.max * jitter,
          sum: a.sum * jitter * (n / SAMPLES_PER_BUCKET),
          n,
          gap,
          rangeMask: a.rangeMask,
          logicAny: n ? a.logicAny : 0,
          logicAll: n ? a.logicAll : 0,
          edges: n ? a.edges : 0,
        });
        t += bucketSeconds;
      }
      for (let i = 0; i < cycleHist.length; i++) this.decimator.histogram[i] += cycleHist[i]!;
      if (missing > 0) {
        this.decimator.gapCount++;
        this.decimator.totalMissing += missing;
        this.decimator.totalStored += CYCLE_SAMPLES - missing;
        this.log(
          "loss",
          "sample_gap",
          "ev_gap",
          [fmtInt(missing), fmtInt(Math.round((t + PREFILL_S) * SAMPLE_RATE_HZ))],
          undefined,
          t - cycleSeconds,
        );
      } else {
        this.decimator.totalStored += CYCLE_SAMPLES;
      }
    }
  }

  // -- clock --------------------------------------------------------------

  now(): number {
    return this.t;
  }
  historyStart(): number {
    return -PREFILL_S;
  }

  isStreaming(): boolean {
    return this.streaming;
  }
  startStream(): void {
    if (this.streaming) return;
    this.streaming = true;
    this.log("info", "start_measuring", "ev_restart", []);
    this.notify();
  }
  stopStream(): void {
    if (!this.streaming) return;
    this.streaming = false;
    this.decimator.breakContinuity();
    this.log("info", "stop_measuring", "ev_stop", []);
    this.notify();
  }

  private lastTickMs: number | null = null;

  tick(nowMs: number): void {
    if (this.lastTickMs === null) {
      this.lastTickMs = nowMs;
      return;
    }
    const dt = Math.min((nowMs - this.lastTickMs) / 1000, 0.25);
    this.lastTickMs = nowMs;
    if (!this.streaming) return;

    const cyc = this.cycle();
    const noise = this.activeProfile === "demo" ? 0.004 : 0;
    let remaining = Math.round(dt * SAMPLE_RATE_HZ);
    while (remaining > 0) {
      if (this.pendingLoss > 0) {
        const take = Math.min(this.pendingLoss, remaining, 4096);
        this.decimator.pushGap(this.t, take);
        this.pendingLoss -= take;
        this.sampleIndex += take;
        this.t += take / SAMPLE_RATE_HZ;
        remaining -= take;
        continue;
      }
      const i = this.sampleIndex % CYCLE_SAMPLES;
      let v = cyc.current[i]!;
      if (noise) v *= 1 + (this.rnd() - 0.5) * noise;
      this.decimator.pushSample(this.t, v, rangeFor(v), cyc.logic[i]!);
      this.sampleIndex++;
      this.t += 1 / SAMPLE_RATE_HZ;
      remaining--;
    }
  }

  // -- state --------------------------------------------------------------

  private voltageBasis(): VoltageBasis {
    if (this.assumedVoltageMv !== null) return "caller_override";
    return this.mode === Mode.SOURCE ? "configured_source" : "device_metadata";
  }

  private deviceState(): DeviceState {
    return {
      mode: this.mode,
      source_voltage_mv: this.voltageMv,
      dut_power: this.dutPower,
      measuring: this.streaming,
      source_voltage_basis: this.voltageBasis(),
    };
  }

  private calibration(): Calibration {
    return {
      calibrated: true,
      hw: 2,
      ia: 0,
      vdd_mv: this.vddMv,
      ranges: SIM_CAL_R.map((r) => ({ r, gs: 0, gi: 1, o: 0, s: 0, i: 0, ug: 1 })),
      missing_ranges: [],
      terminated: true,
    };
  }

  snapshot(): DeviceSnapshot {
    if (!this.snapshotCache) {
      this.snapshotCache = {
        info: {
          serial_number: "SIM0001",
          vid: 0x1915,
          pid: 0xc00a,
          firmware_version: "1.2.4-sim",
          simulated: true,
          measurement_port: "simulated:SIM0001",
          fingerprint: "HW=2 IA=0 keys=38",
        },
        state: this.deviceState(),
        calibration: this.calibration(),
        assumedVoltageMv: this.assumedVoltageMv,
        maxVoltageMv: this.maxVoltageMv,
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
    // New identities so `useSyncExternalStore` sees a change; the decimator is
    // deliberately not part of this — it changes 100,000 times a second.
    this.snapshotCache = null;
    this.eventsCache = this.eventLog.slice();
    for (const l of this.listeners) l();
  }

  private log(
    kind: EventKind,
    operation: string,
    messageKey: MessageKey,
    args: (string | number)[],
    delta?: ConsoleEvent["delta"],
    at?: number,
    observed?: boolean,
  ): void {
    const event: ConsoleEvent = {
      id: this.nextEventId++,
      kind,
      operation,
      messageKey,
      args,
      t: at ?? this.t,
    };
    if (delta) event.delta = delta;
    if (observed !== undefined) event.observed = observed;
    this.eventLog.push(event);
    if (this.eventLog.length > 400) this.eventLog.splice(0, this.eventLog.length - 400);
    this.eventsCache = this.eventLog.slice();
  }

  // -- control ------------------------------------------------------------

  private describe(): Record<string, string> {
    return {
      mode: this.mode === Mode.SOURCE ? "source" : "ampere",
      source_voltage_mv: String(this.voltageMv),
      dut_power: String(this.dutPower),
      measuring: String(this.streaming),
    };
  }

  private project(request: ControlRequest): Record<string, string> {
    const after = this.describe();
    switch (request.kind) {
      case "mode":
        after["mode"] = request.mode === Mode.SOURCE ? "source" : "ampere";
        break;
      case "voltage":
        after["source_voltage_mv"] = String(request.voltageMv);
        break;
      case "dut-power":
        after["dut_power"] = String(request.on);
        break;
    }
    return after;
  }

  private validate(request: ControlRequest): void {
    if (request.kind !== "voltage") return;
    const mv = request.voltageMv;
    if (!Number.isFinite(mv) || mv < VOLTAGE_MIN_MV || mv > VOLTAGE_MAX_MV) {
      throw new ControlRejected("er_range");
    }
    if (this.maxVoltageMv !== null && mv > this.maxVoltageMv) {
      throw new ControlRejected("er_ceiling", [mv, this.maxVoltageMv]);
    }
  }

  /** Operations the device refuses while a stream is open (`device.py`). */
  static interrupts(request: ControlRequest): boolean {
    return request.kind === "mode" || request.kind === "voltage";
  }

  private operationName(request: ControlRequest): string {
    return request.kind === "mode"
      ? "set_mode"
      : request.kind === "voltage"
        ? "set_source_voltage_mv"
        : "set_dut_power";
  }

  async preview(request: ControlRequest): Promise<StateChange> {
    this.validate(request);
    const warnings = ["W_DRY_RUN"];
    if (request.kind === "dut-power") {
      warnings.push("W_STATE_UNVERIFIED");
      if (request.on) warnings.push("W_DUT_POWER_TRANSIENT");
    }
    if (request.kind === "mode" && request.mode === Mode.AMPERE) warnings.push("W_VOLTAGE_ASSUMED");
    return {
      operation: this.operationName(request),
      requested: { ...request },
      before: this.describe(),
      after: this.project(request),
      applied: false,
      observed_after: false,
      warnings,
    };
  }

  async apply(request: ControlRequest): Promise<StateChange> {
    this.validate(request);
    if (SimulatedSource.interrupts(request) && this.streaming) {
      // Mode, voltage, reset and user gain all require no active stream and no
      // measurement in progress, so the supervisor stops, applies, reopens.
      this.log("info", "live_interrupted_for_control", "ev_interrupt", [this.operationName(request)]);
      this.decimator.breakContinuity();
    }
    const change = await this.applyOne(request);
    this.notify();
    return change;
  }

  /** One state change, logged. Sequencing and notification stay with the caller. */
  private async applyOne(request: ControlRequest): Promise<StateChange> {
    const before = this.describe();
    const after = this.project(request);

    let observed: boolean;
    const warnings: string[] = [];
    switch (request.kind) {
      case "mode":
        this.mode = request.mode;
        observed = true; // read back from metadata
        break;
      case "voltage":
        this.voltageMv = request.voltageMv;
        this.vddMv = request.voltageMv;
        observed = true;
        break;
      case "dut-power":
        this.dutPower = request.on;
        // This hardware cannot report its power state back. Requested is not
        // confirmed, and the console must not round that up.
        observed = false;
        this.dutObserved = false;
        warnings.push("W_STATE_UNVERIFIED");
        if (request.on) warnings.push("W_DUT_POWER_TRANSIENT");
        break;
    }

    const change: StateChange = {
      operation: this.operationName(request),
      requested: { ...request },
      before,
      after,
      applied: true,
      observed_after: observed,
      warnings,
    };

    const delta: Record<string, { from: string; to: string }> = {};
    for (const k of Object.keys(before)) {
      delta[k] = { from: before[k]!, to: after[k]! };
    }
    this.log("state", change.operation, "ev_applied", [], delta, undefined, observed);
    for (const w of warnings) {
      this.log("warn", w, `w_${w.slice(2).toLowerCase()}` as MessageKey, []);
    }
    return change;
  }

  config(): DeviceConfig {
    return { mode: this.mode, voltageMv: this.voltageMv };
  }

  outputOn(): boolean {
    return this.dutPower === true;
  }

  /**
   * Run a staged plan in the only order that is safe.
   *
   * VOUT comes down first: changing the source voltage while the DUT is drawing
   * from it changes what the DUT receives, live, and no confirmation dialog
   * makes that acceptable. Then measuring stops, because the device refuses a
   * mode or voltage change while it is streaming. The settings land, the stream
   * comes back, and the output returns only if the operator asked for it.
   */
  async applyPlan(plan: ApplyPlan): Promise<StateChange[]> {
    for (const change of plan.changes) this.validate(change);
    const applied: StateChange[] = [];
    const wasStreaming = this.streaming;

    if (plan.stopOutputFirst && this.dutPower) {
      applied.push(await this.applyOne({ kind: "dut-power", on: false }));
    }
    if (plan.interruptsStream && wasStreaming) {
      this.streaming = false;
      this.decimator.breakContinuity();
      this.log("info", "stop_measuring", "ev_interrupt", ["set_mode / set_source_voltage_mv"]);
    }
    for (const change of plan.changes) {
      applied.push(await this.applyOne(change));
    }
    if (plan.interruptsStream && wasStreaming) {
      this.streaming = true;
      this.log("info", "start_measuring", "ev_restart", []);
    }
    if (plan.restartOutput) {
      applied.push(await this.applyOne({ kind: "dut-power", on: true }));
    }
    this.notify();
    return applied;
  }

  setAssumedVoltageMv(mv: number | null): void {
    this.assumedVoltageMv = mv;
    this.notify();
  }
  setMaxVoltageMv(mv: number | null): void {
    this.maxVoltageMv = mv;
    this.notify();
  }

  readonly demo: DemoControls = {
    setProfile: (profile) => {
      if (profile === this.activeProfile) return;
      this.activeProfile = profile;
      // A different DUT is a different session: the old history describes a
      // load that is no longer connected, so it is rebuilt rather than joined.
      const fresh = new Decimator();
      Object.assign(this.decimator, fresh);
      this.t = 0;
      this.sampleIndex = 0;
      this.eventLog = [];
      this.prefill();
      this.log("info", "profile_changed", "ev_profile", [profile === "demo" ? "sc_demo" : "sc_idle"]);
      this.notify();
    },
    profile: () => this.activeProfile,
    injectLoss: (samples) => {
      this.pendingLoss += samples;
      this.log("loss", "sample_gap", "ev_gap_inject", [fmtInt(samples)]);
      this.log("warn", "W_SAMPLE_GAPS", "w_sample_gaps", []);
      this.notify();
    },
  };

  /** Only meaningful for the DUT-power card; kept out of `DeviceState`. */
  dutPowerObserved(): boolean {
    return this.dutObserved;
  }
}
