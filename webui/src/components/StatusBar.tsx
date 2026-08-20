import { fmtPercent } from "../core/format";
import type { DataSource, DeviceSnapshot } from "../data/source";
import { useFlash, useTicker } from "../hooks";
import { useI18n } from "../i18n";
import { Mode } from "../types";

export type DrawerPane = "mode" | "voltage" | "dut" | "stream" | "cal";

interface StatusBarProps {
  source: DataSource;
  snapshot: DeviceSnapshot;
  onOpen: (pane: DrawerPane) => void;
}

type Tone = "good" | "warn" | "bad" | "acc" | "unk" | "none";

/**
 * The whole device state on one line.
 *
 * Label, value, badge — nothing else. What each value means, why a voltage is a
 * setpoint rather than a reading, and every control that changes it live one
 * click away in the drawer. Keeping the explanation off this line is what lets
 * the trace have the rest of the window.
 */
export function StatusBar({ source, snapshot, onOpen }: StatusBarProps) {
  const { t } = useI18n();
  useTicker(4); // stored/coverage move constantly; four reads a second is plenty
  const { state } = snapshot;

  const isSource = state.mode === Mode.SOURCE;
  const shownVoltage = snapshot.assumedVoltageMv ?? state.source_voltage_mv;
  const dut = state.dut_power;
  const dec = source.decimator;
  const clean = dec.gapCount === 0;

  return (
    <div className="statusbar">
      <Cell
        label={t("t_mode")}
        value={t(isSource ? "m_source" : "m_ampere")}
        badge={isSource ? "SOURCE" : "AMPERE"}
        tone="acc"
        hint={t(isSource ? "m_source_hint" : "m_ampere_hint")}
        onClick={() => onOpen("mode")}
      />
      <Cell
        label={t("t_voltage")}
        value={`${shownVoltage ?? "—"} mV`}
        badge={state.source_voltage_basis}
        tone={state.source_voltage_basis === "device_metadata" ? "warn" : "acc"}
        hint={t(isSource ? "v_hint_src" : "v_hint_amp")}
        onClick={() => onOpen("voltage")}
      />
      <Cell
        label={t("t_dut")}
        value={t(dut === null ? "d_unknown_word" : dut ? "d_on_word" : "d_off_word")}
        badge={dut === null ? "UNKNOWN" : dut ? "ON" : "OFF"}
        tone={dut === null ? "unk" : dut ? "warn" : "none"}
        hint={t(dut ? "d_hint_on" : "d_hint_off")}
        onClick={() => onOpen("dut")}
      />
      <Cell
        label={t("t_stream")}
        value={fmtPercent(dec.coveredFraction())}
        badge={clean ? "COMPLETE" : "W_SAMPLE_GAPS"}
        tone={clean ? "good" : "bad"}
        hint={t("s_hint", dec.gapCount, dec.totalStored.toLocaleString("en-US"))}
        onClick={() => onOpen("stream")}
      />
      <Cell
        label={t("t_cal")}
        value={`${snapshot.calibration.ranges.length} / ${snapshot.calibration.ranges.length}`}
        badge="CALIBRATED"
        tone="good"
        hint={t("c_hint")}
        onClick={() => onOpen("cal")}
      />
    </div>
  );
}

/**
 * `hint` is a native tooltip, not a visible line.
 *
 * The console still has to say that a voltage is a setpoint and that DUT power
 * was requested rather than confirmed — but it costs no vertical space to put
 * that on hover, and the badge already carries the state at a glance.
 */
function Cell({
  label,
  value,
  badge,
  tone,
  hint,
  onClick,
}: {
  label: string;
  value: string;
  badge: string;
  tone: Tone;
  hint: string;
  onClick: () => void;
}) {
  const { t } = useI18n();
  const flash = useFlash(badge);
  return (
    <button
      type="button"
      className="statuscell"
      onClick={onClick}
      title={`${label} — ${hint}`}
      aria-label={`${label}: ${value}. ${hint}. ${t("t_open")}`}
    >
      <span className="lbl">{label}</span>
      <span className="val">{value}</span>
      <span className={`pill${tone === "none" ? "" : ` ${tone}`}${flash ? " flash" : ""}`}>{badge}</span>
    </button>
  );
}
