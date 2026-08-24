import { fmtPercent, fmtRelTime } from "../core/format";
import { modeLabels } from "../core/mode";
import type { ConnectionStatus, DataSource, DeviceSnapshot } from "../data/source";
import { useFlash, useTicker } from "../hooks";
import { useI18n } from "../i18n";
import { Mode } from "../types";

export type DrawerPane = "mode" | "voltage" | "dut" | "stream" | "cal";

interface StatusBarProps {
  source: DataSource;
  snapshot: DeviceSnapshot;
  onOpen: (pane: DrawerPane) => void;
  /** Reconnect now rather than waiting out the backoff. */
  onRetry: () => void;
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
export function StatusBar({ source, snapshot, onOpen, onRetry }: StatusBarProps) {
  const { t } = useI18n();
  useTicker(4); // stored/coverage move constantly; four reads a second is plenty
  const { state } = snapshot;

  const mode = modeLabels(state.mode);
  const isSource = state.mode === Mode.SOURCE;
  const shownVoltage = snapshot.assumedVoltageMv ?? state.source_voltage_mv;
  const dut = state.dut_power;
  const dec = source.decimator;
  const clean = dec.gapCount === 0;

  return (
    <div className="statusbar">
      {!source.simulated && snapshot.connection.phase !== "open" && (
        <FrozenBanner connection={snapshot.connection} onRetry={onRetry} />
      )}
      <Cell
        label={t("t_mode")}
        value={t(mode.name)}
        badge={mode.badge}
        tone={mode.tone}
        hint={t(mode.hint)}
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
 * Where the trace stopped being live, said in place.
 *
 * The rail already carries a badge; this says the one thing a badge cannot,
 * which is *when* the values beside it were last true. They are still the last
 * thing the server said and none of them is being refreshed -- reading them is
 * legitimate, believing they are current is not.
 *
 * The button is here rather than in the rail because this is where the reader
 * is looking when they notice: the backoff runs to tens of seconds, and
 * somebody who has just plugged the cable back in should not wait it out.
 */
function FrozenBanner({
  connection,
  onRetry,
}: {
  connection: ConnectionStatus;
  onRetry: () => void;
}) {
  const { t } = useI18n();
  // `frozenAt` is null only when this console has never been open, so there is
  // no moment to point at and nothing to say froze. Both sentences are plain
  // text in all four catalogues, and neither takes a placeholder -- which is
  // why the timestamp sits beside the message rather than inside it.
  const frozen = connection.frozenAt !== null;
  return (
    <div className="connbanner" role="status">
      <span className="txt">{t(frozen ? "conn_frozen" : "conn_hint_closed")}</span>
      {frozen && <span className="val">{fmtRelTime(connection.frozenAt!)}</span>}
      <button type="button" className="btn sm" onClick={onRetry}>
        {t("conn_retry")}
      </button>
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
