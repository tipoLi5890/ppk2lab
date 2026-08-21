import { useState } from "react";

import type { WindowStats } from "../chart/columns";
import {
  FULL_SCALE_UA,
  SAMPLE_RATE_HZ,
  TIER_CONDITIONAL_MIN,
  TIER_EXPERIMENTAL_MIN,
  TIER_VALIDATED_MIN,
} from "../core/constants";
import { fmtCurrent, fmtInt, fmtPercent } from "../core/format";
import { histQuantile } from "../core/histogram";
import type { DataSource, DeviceSnapshot } from "../data/source";
import { useTicker } from "../hooks";
import { useI18n, type MessageKey } from "../i18n";
import { Mode } from "../types";
import { Icon } from "./Icons";

/**
 * Inspector panels.
 *
 * Nothing here prints a paragraph. Where a number needs a caveat it carries a
 * coded badge — the same vocabulary the JSON envelopes publish — and the badge's
 * tooltip holds the sentence. The badge is the part that must be seen; the
 * sentence is the part that must be available.
 */

/** A coded caveat. Visible as a chip, explained on hover. */
function Badge({ code, k, tone = "warn" }: { code: string; k: MessageKey; tone?: string }) {
  const { t } = useI18n();
  return (
    <span className={`pill ${tone}`} title={t(k).replace(/<[^>]+>/g, "")}>
      {code}
    </span>
  );
}

function Stat({
  label,
  value,
  badge,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  badge?: React.ReactNode;
  tone?: "loss" | undefined;
}) {
  return (
    <div className="tile">
      <span className="lbl">{label}</span>
      <div className="v" style={tone ? { color: "var(--loss)" } : undefined}>
        {value}
      </div>
      {badge ? <div className="badges">{badge}</div> : null}
    </div>
  );
}

const QUANTILES: [number, string][] = [
  [0.05, "p5"],
  [0.5, "p50"],
  [0.9, "p90"],
  [0.95, "p95"],
  [0.99, "p99"],
  [0.999, "p999"],
];

export function StatsPanel({
  source,
  snapshot,
  readStats,
}: {
  source: DataSource;
  snapshot: DeviceSnapshot;
  readStats: () => WindowStats | null;
}) {
  const { t } = useI18n();
  useTicker(4);

  const stats = readStats();
  const hist = source.decimator.histogram;
  const p50 = histQuantile(hist, 0.5);

  // An unknown mode is not Source. That is the conservative reading and the
  // only honest one: without knowing the mode there is no basis for saying the
  // PPK2 supplies the DUT, so the voltage stays null and energy stays null
  // rather than being computed from a setpoint that may power nothing.
  const isSource = snapshot.state.mode === Mode.SOURCE;
  const voltageMv = snapshot.assumedVoltageMv ?? (isSource ? snapshot.state.source_voltage_mv : null);

  const chargeUc = stats ? stats.mean * stats.spanSeconds : null;
  const lowerBound = stats ? stats.gap > 0 : false;
  const saturated = stats ? stats.max >= FULL_SCALE_UA[4]! * 0.999 : false;

  return (
    <div className="cols">
      <div>
        <span className="lbl">{t("st_win")}</span>
        <div className="tiles" style={{ marginTop: 8 }}>
          <Stat label={t("st_mean")} value={stats ? fmtCurrent(stats.mean) : "—"} />
          <Stat
            label={t("st_p50")}
            value={`${p50.atFloor ? "≤ " : ""}${fmtCurrent(p50.value)}`}
            badge={p50.atFloor ? <Badge code="W_BELOW_MEASUREMENT_FLOOR" k="st_floor_note" /> : undefined}
          />
          <Stat label={t("st_min")} value={stats ? fmtCurrent(stats.min) : "—"} />
          <Stat
            label={t("st_max")}
            value={stats ? fmtCurrent(stats.max) : "—"}
            badge={saturated ? <Badge code="saturated" k="st_max_sat" tone="bad" /> : undefined}
            tone={saturated ? "loss" : undefined}
          />
        </div>

        <span className="lbl block">{t("st_dist")}</span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("st_q")}</th>
                <th>{t("st_val")}</th>
                <th>{t("st_src")}</th>
              </tr>
            </thead>
            <tbody>
              {QUANTILES.map(([q, name]) => {
                const r = histQuantile(hist, q);
                return (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>
                      {r.atFloor ? "≤ " : ""}
                      {fmtCurrent(r.value)}
                    </td>
                    <td>
                      <span
                        className={`pill ${r.atFloor ? "warn" : "good"}`}
                        title={r.atFloor ? t("st_floor_note").replace(/<[^>]+>/g, "") : undefined}
                      >
                        {t(r.atFloor ? "st_floor" : "st_measured")}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <span className="lbl">{t("st_ce")}</span>
        <div className="tiles" style={{ marginTop: 8 }}>
          <Stat
            label="charge_uc"
            value={chargeUc === null ? "—" : `${chargeUc.toFixed(1)} µC`}
            badge={lowerBound ? <Badge code="charge_is_lower_bound" k="st_lowerb" /> : undefined}
            tone={lowerBound ? "loss" : undefined}
          />
          <Stat
            label="energy_uj"
            value={
              voltageMv === null || chargeUc === null ? "null" : `${(chargeUc * (voltageMv / 1000)).toFixed(0)} µJ`
            }
            tone={voltageMv === null ? "loss" : undefined}
            badge={
              voltageMv === null ? (
                <Badge code="ampere: not computable" k="st_e_null_note" tone="bad" />
              ) : (
                <span className="pill acc" title={t("st_e_ok_note", snapshot.state.source_voltage_basis, "")}>
                  {snapshot.state.source_voltage_basis}
                </span>
              )
            }
          />
        </div>

        <span className="lbl block" title={t("st_err_note").replace(/<[^>]+>/g, "")}>
          {t("st_err")}
        </span>
        <div className="tablewrap">
          <table>
            <tbody>
              <tr>
                <td>mean_ua_typical</td>
                <td>{stats ? `± ${fmtCurrent(typicalError(stats))}` : "—"}</td>
                <td>{t("st_sys")}</td>
              </tr>
              <tr>
                <td>mean_ua_batch_stderr</td>
                <td>{stats ? `± ${fmtCurrent(Math.abs(stats.mean) * 0.0009)}` : "—"}</td>
                <td>{t("st_stat")}</td>
              </tr>
              <tr>
                <td>guaranteed</td>
                <td style={{ color: "var(--loss)" }}>false</td>
                <td>
                  <Badge code="typical" k="st_err_note" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <span className="lbl block">{t("st_int")}</span>
        <dl className="kv">
          <dt>complete</dt>
          <dd style={{ color: lowerBound ? "var(--loss)" : "var(--good)" }}>{String(!lowerBound)}</dd>
          <dt>covered_fraction</dt>
          <dd>{stats ? fmtPercent(stats.covered) : "—"}</dd>
          <dt>missing_known</dt>
          <dd>{stats ? fmtInt(stats.gap) : "—"}</dd>
          <dt>saturated_samples</dt>
          <dd>0</dd>
        </dl>
      </div>

      <div>
        <span className="lbl" title={t("st_rshare_note").replace(/<[^>]+>/g, "")}>
          {t("st_rshare")}
        </span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("st_range")}</th>
                <th>{t("st_fs")}</th>
                <th title={t("st_rshare_note").replace(/<[^>]+>/g, "")}>{t("st_share")}</th>
              </tr>
            </thead>
            <tbody>
              {FULL_SCALE_UA.map((fs, i) => (
                <tr key={i}>
                  <td>
                    <span className="rangeswatch" style={{ background: `var(--r${i})` }} />
                    {i}
                  </td>
                  <td>{fmtCurrent(fs, 0)}</td>
                  <td>
                    {stats ? fmtPercent(stats.rangeColumns[i]! / Math.max(1, stats.columnsWithData), 1) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/** Nordic's typical per-range figure: ±10%, ±15% on the top range. */
function typicalError(stats: WindowStats): number {
  const topShare = stats.rangeColumns[4]! / Math.max(1, stats.columnsWithData);
  return Math.abs(stats.mean) * (0.1 * (1 - topShare) + 0.15 * topShare);
}

const TRIGGER_SPECS: Record<string, string> = {
  current: "current>10mA",
  digital: "digital D3=1,D6=0",
  edge: "digital D3 rising",
  uart: 'uart D0 9600 "BOOT"',
  spi: "spi 0x9f",
};

function Field({
  label,
  defaultValue,
  type = "text",
  placeholder,
  title,
}: {
  label: string;
  defaultValue: string;
  type?: string;
  placeholder?: string;
  title?: string;
}) {
  return (
    <div className="field" style={{ flex: 1 }} title={title}>
      <span className="lbl">{label}</span>
      <input type={type} defaultValue={defaultValue} placeholder={placeholder} />
    </div>
  );
}

export function TriggerPanel() {
  const { t } = useI18n();
  const [kind, setKind] = useState("current");
  const [spec, setSpec] = useState(TRIGGER_SPECS["current"]!);
  const plain = (k: MessageKey) => t(k).replace(/<[^>]+>/g, "");

  return (
    <div className="cols">
      <div>
        <div className="field">
          <span className="lbl">{t("tg_type")}</span>
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setSpec(TRIGGER_SPECS[e.target.value] ?? "");
            }}
          >
            {Object.entries(TRIGGER_SPECS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <span className="lbl">{t("tg_spec")}</span>
          <input type="text" value={spec} onChange={(e) => setSpec(e.target.value)} />
        </div>
        <div className="row">
          <Field label={t("tg_pre")} defaultValue="0" title={plain("tg_tl_note")} />
          <Field label={t("tg_post")} defaultValue="1s" />
          <Field label={t("tg_hold")} defaultValue="1" type="number" />
          <Field label={t("tg_timeout")} defaultValue="" placeholder="30s" />
        </div>
        <label className="check">
          <input type="checkbox" />
          <span>--allow-experimental</span>
        </label>
      </div>

      <div>
        <span className="lbl">{t("tg_status")}</span>
        <dl className="kv">
          <dt>armed</dt>
          <dd>false</dd>
          <dt>fired_index</dt>
          <dd>null</dd>
          <dt>pre_samples</dt>
          <dd>0</dd>
          <dt>post_samples</dt>
          <dd>{fmtInt(SAMPLE_RATE_HZ)}</dd>
        </dl>
        <div className="badges">
          <Badge code="uart: needs idle" k="tg_uart_note" />
          <Badge code="at= incompatible" k="tg_tl_note" />
        </div>
      </div>
    </div>
  );
}

function tierOf(samplesPerUnit: number): { tier: string; confidence: number; tone: string } {
  if (samplesPerUnit >= TIER_VALIDATED_MIN) return { tier: "validated", confidence: 1.0, tone: "good" };
  if (samplesPerUnit >= TIER_CONDITIONAL_MIN) return { tier: "conditional", confidence: 0.7, tone: "warn" };
  if (samplesPerUnit >= TIER_EXPERIMENTAL_MIN) return { tier: "experimental", confidence: 0.4, tone: "warn" };
  return { tier: "unsupported", confidence: 0.0, tone: "bad" };
}

export function DecoderPanel() {
  const { t } = useI18n();
  const [baud, setBaud] = useState(9600);
  const [sclkHz, setSclkHz] = useState(5000);
  const plain = (k: MessageKey) => t(k).replace(/<[^>]+>/g, "");

  const uart = tierOf(SAMPLE_RATE_HZ / Math.max(1, baud));
  const spi = tierOf(SAMPLE_RATE_HZ / Math.max(1, sclkHz));

  const badges = (info: ReturnType<typeof tierOf>, unit: string) => (
    <div className="badges">
      <span
        className={`pill ${info.tone}`}
        title={
          info.tier === "unsupported"
            ? plain("dc_unsup_note")
            : info.tier === "experimental"
              ? plain("dc_exp_note")
              : plain("dc_note")
        }
      >
        {info.tier}
      </span>
      <span className="pill">{unit}</span>
      <span className="pill">confidence {info.confidence.toFixed(1)}</span>
    </div>
  );

  return (
    <div className="cols">
      <div>
        <span className="lbl">{t("dc_uart")}</span>
        <div className="row" style={{ marginTop: 8 }}>
          <div className="field" style={{ flex: 1 }}>
            <span className="lbl">{t("dc_ch")}</span>
            <select defaultValue="D0">
              {["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"].map((d) => (
                <option key={d}>{d}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <span className="lbl">baud</span>
            <input type="number" step={100} value={baud} onChange={(e) => setBaud(Number(e.target.value))} />
          </div>
          <Field label={t("dc_bits")} defaultValue="8" type="number" />
          <Field label={t("dc_stop")} defaultValue="1" type="number" />
        </div>
        {badges(uart, t("dc_spb_unit", (SAMPLE_RATE_HZ / Math.max(1, baud)).toFixed(2)))}
      </div>

      <div>
        <span className="lbl">{t("dc_spi")}</span>
        <div className="row" style={{ marginTop: 8 }}>
          <Field label="SCLK" defaultValue="D3" />
          <Field label="MOSI" defaultValue="D4" />
          <Field label="MISO" defaultValue="D5" />
          <Field label="CS" defaultValue="D6" />
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <span className="lbl">{t("dc_hz")}</span>
            <input type="number" step={500} value={sclkHz} onChange={(e) => setSclkHz(Number(e.target.value))} />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <span className="lbl">mode</span>
            <select defaultValue="0">
              <option>0</option>
              <option>1</option>
              <option>2</option>
              <option>3</option>
            </select>
          </div>
          <Field label={t("dc_word")} defaultValue="8" type="number" />
        </div>
        {badges(spi, t("dc_spc_unit", (SAMPLE_RATE_HZ / Math.max(1, sclkHz)).toFixed(2)))}
      </div>

      <div>
        <span className="lbl" title={plain("dc_note")}>
          {t("dc_tiers")}
        </span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("dc_tier")}</th>
                <th>{t("dc_spb")}</th>
                <th>{t("dc_conf")}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>validated</td>
                <td>≥ {TIER_VALIDATED_MIN}</td>
                <td>1.0</td>
              </tr>
              <tr>
                <td>conditional</td>
                <td>≥ {TIER_CONDITIONAL_MIN}</td>
                <td>0.7</td>
              </tr>
              <tr>
                <td>experimental</td>
                <td>≥ {TIER_EXPERIMENTAL_MIN}</td>
                <td>0.4</td>
              </tr>
              <tr>
                <td>unsupported</td>
                <td>&lt; {TIER_EXPERIMENTAL_MIN}</td>
                <td>{t("dc_refuse")}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function parseDuration(s: string): number | null {
  const m = /^([\d.]+)\s*(ms|s|m)?$/.exec(s.trim());
  if (!m) return null;
  const v = Number.parseFloat(m[1]!);
  return m[2] === "ms" ? v / 1000 : m[2] === "m" ? v * 60 : v;
}

export function RecordPanel({ unlocked }: { unlocked: boolean }) {
  const { t } = useI18n();
  const [duration, setDuration] = useState("30s");
  const seconds = parseDuration(duration) ?? 30;
  const plain = (k: MessageKey) => t(k).replace(/<[^>]+>/g, "");

  return (
    <div className="cols">
      <div>
        <span className="lbl" title={plain("rc_never")}>
          {t("rc_title")}
        </span>
        <div className="row" style={{ marginTop: 8 }}>
          <div className="field" style={{ flex: 1 }}>
            <span className="lbl">{t("rc_dur")}</span>
            <input type="text" value={duration} onChange={(e) => setDuration(e.target.value)} />
          </div>
          <Field label={t("rc_or")} defaultValue="" type="number" />
        </div>
        <Field label={t("rc_out")} defaultValue="run.ppk2a" />
        <Field label={t("rc_tags")} defaultValue="sn=POD01, fw=0.3.0" />
        {unlocked ? (
          <button type="button" className="btn primary" style={{ marginTop: 6 }}>
            <Icon name="record" />
            <span>{t("rc_start")}</span>
          </button>
        ) : (
          <p className="lockedhint">
            <Icon name="lock" />
            <span>{t("rc_locked")}</span>
          </p>
        )}
      </div>

      <div>
        <span className="lbl" title={plain("rc_size_note")}>
          {t("rc_size")}
        </span>
        <div className="tiles" style={{ marginTop: 8 }}>
          <Stat label={t("rc_raw")} value={`${((seconds * SAMPLE_RATE_HZ * 4) / 1e6).toFixed(1)} MB`} />
          <Stat label={t("rc_cnt")} value={fmtInt(Math.round(seconds * SAMPLE_RATE_HZ))} />
        </div>
        <div className="badges">
          <Badge code="CAPTURE_TOO_LARGE > 25M" k="rc_size_note" />
        </div>
      </div>

      <div>
        <span className="lbl">{t("rc_recent")}</span>
        <dl className="kv">
          <dt>—</dt>
          <dd>{t("rc_none")}</dd>
        </dl>
      </div>
    </div>
  );
}

const COMMANDS: [string, MessageKey][] = [
  ["discover", "cp_ro"],
  ["info", "cp_ro"],
  ["capabilities", "cp_ro"],
  ["schema", "cp_ro"],
  ["doctor", "cp_ro_def"],
  ["configure", "cp_sc"],
  ["capture", "cp_meas"],
  ["inspect", "cp_off"],
  ["decode", "cp_off"],
  ["measure", "cp_off"],
  ["assert", "cp_off"],
  ["compare", "cp_off"],
  ["export", "cp_off"],
];

const WARNINGS: [string, string][] = [
  ["W_SAMPLE_GAPS", "capture integrity"],
  ["W_TIMELINE_COMPRESSION", "capture integrity"],
  ["W_UNACCOUNTED_SAMPLES", "capture integrity"],
  ["W_PARTIAL_INTEGRITY", "capture integrity"],
  ["W_VOLTAGE_ASSUMED", "measurement trust"],
  ["W_BELOW_MEASUREMENT_FLOOR", "measurement trust"],
  ["W_NOT_CALIBRATED", "measurement trust"],
  ["W_CLIPPED", "measurement trust"],
  ["W_DUT_POWER_UNKNOWN", "device state"],
  ["W_DUT_POWER_TRANSIENT", "device state"],
  ["W_STATE_UNVERIFIED", "device state"],
  ["W_DRY_RUN", "device state"],
  ["W_DECODER_RATE", "analysis"],
  ["W_WINDOW_UNPOPULATED", "analysis"],
  ["W_INSTRUMENT_MISMATCH", "analysis"],
];

const EXIT_CODES: [number, MessageKey][] = [
  [0, "cp_e0"],
  [1, "cp_e1"],
  [2, "cp_e2"],
  [6, "cp_e6"],
];

export function CapabilitiesPanel() {
  const { t } = useI18n();
  const plain = (k: MessageKey) => t(k).replace(/<[^>]+>/g, "");
  return (
    <div className="cols">
      <div>
        <span className="lbl">{t("cp_cmds")}</span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("cp_cmd")}</th>
                <th>{t("cp_hw")}</th>
              </tr>
            </thead>
            <tbody>
              {COMMANDS.map(([name, key]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td className="txt">{t(key)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <span className="lbl" title={plain("cp_warn_note")}>
          {t("cp_warns")}
        </span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("cp_code")}</th>
                <th>{t("cp_cat")}</th>
              </tr>
            </thead>
            <tbody>
              {WARNINGS.map(([code, category]) => (
                <tr key={code}>
                  <td>{code}</td>
                  <td className="txt">{category}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <span className="lbl" title={plain("cp_exit_note")}>
          {t("cp_exit")}
        </span>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>{t("cp_exitcode")}</th>
                <th>{t("cp_meaning")}</th>
              </tr>
            </thead>
            <tbody>
              {EXIT_CODES.map(([code, key]) => (
                <tr key={code}>
                  <td>{code}</td>
                  <td className="txt">{t(key)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
