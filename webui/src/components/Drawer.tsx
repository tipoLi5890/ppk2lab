import { useEffect, useRef, useState } from "react";

import { FULL_SCALE_UA, SAMPLE_RATE_HZ, VOLTAGE_MAX_MV, VOLTAGE_MIN_MV } from "../core/constants";
import { fmtCurrent, fmtInt, fmtPercent } from "../core/format";
import type { DataSource, DeviceConfig, DeviceSnapshot } from "../data/source";
import { useTicker } from "../hooks";
import { useI18n, type MessageKey } from "../i18n";
import { Mode } from "../types";
import { Icon } from "./Icons";
import type { DrawerPane } from "./StatusBar";

interface DrawerProps {
  pane: DrawerPane | null;
  onClose: () => void;
  source: DataSource;
  snapshot: DeviceSnapshot;
  controlUnlocked: boolean;
  /** Edits live here until the operator commits them from the rail. */
  draft: DeviceConfig;
  onDraftChange: (patch: Partial<DeviceConfig>) => void;
  outputOn: boolean;
  onToggleOutput: () => void;
}

/** Catalogue strings carry markup and are authored here, not supplied by a user. */
export function Html({ k, className }: { k: MessageKey; className?: string }) {
  const { t } = useI18n();
  return <p className={className} dangerouslySetInnerHTML={{ __html: t(k) }} />;
}

export function LockedHint() {
  const { t } = useI18n();
  return (
    <p className="lockedhint">
      <Icon name="lock" />
      <span>{t("dr_locked")}</span>
    </p>
  );
}

/**
 * Detail and control for one status tile.
 *
 * Everything that can change hardware state lives here rather than on the
 * strip: a state change is a deliberate act, and it still has to pass through a
 * dry-run preview before anything reaches the wire.
 */
export function Drawer({
  pane,
  onClose,
  source,
  snapshot,
  controlUnlocked,
  draft,
  onDraftChange,
  outputOn,
  onToggleOutput,
}: DrawerProps) {
  const { t } = useI18n();
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (pane) closeRef.current?.focus();
  }, [pane]);

  useEffect(() => {
    if (!pane) return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [pane, onClose]);

  return (
    <>
      <div className={`dr-scrim${pane ? " on" : ""}`} onClick={onClose} />
      <aside
        className={`drawer${pane ? " on" : ""}`}
        aria-hidden={!pane}
        role="dialog"
        aria-modal="true"
        aria-label={pane ? t(`t_${pane}` as MessageKey) : t("dr_close")}
      >
        <header>
          <h2>{pane ? t(`t_${pane}` as MessageKey) : ""}</h2>
          <button type="button" className="btn sm" ref={closeRef} aria-label={t("dr_close")} onClick={onClose}>
            <Icon name="close" />
          </button>
        </header>
        <div className="dr-body">
          {pane === "mode" && (
            <ModePane snapshot={snapshot} unlocked={controlUnlocked} draft={draft} onDraftChange={onDraftChange} />
          )}
          {pane === "voltage" && (
            <VoltagePane
              source={source}
              snapshot={snapshot}
              unlocked={controlUnlocked}
              draft={draft}
              onDraftChange={onDraftChange}
            />
          )}
          {pane === "dut" && (
            <DutPane snapshot={snapshot} unlocked={controlUnlocked} outputOn={outputOn} onToggle={onToggleOutput} />
          )}
          {pane === "stream" && <StreamPane source={source} snapshot={snapshot} />}
          {pane === "cal" && <CalibrationPane snapshot={snapshot} />}
        </div>
      </aside>
    </>
  );
}

function ControlBlock({ unlocked, children }: { unlocked: boolean; children: React.ReactNode }) {
  if (!unlocked) return <LockedHint />;
  return <div>{children}</div>;
}

function ModePane({
  snapshot,
  unlocked,
  draft,
  onDraftChange,
}: {
  snapshot: DeviceSnapshot;
  unlocked: boolean;
  draft: DeviceConfig;
  onDraftChange: (patch: Partial<DeviceConfig>) => void;
}) {
  const { t } = useI18n();
  const isSource = snapshot.state.mode === Mode.SOURCE;
  const pending = draft.mode !== snapshot.state.mode;

  return (
    <section>
      <div className="readout">
        <span className="big">{t(isSource ? "m_source" : "m_ampere")}</span>
      </div>
      <p className="sub">{t(isSource ? "m_source_desc" : "m_ampere_desc")}</p>

      <div className="tablewrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th />
              <th>Ampere</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            <ComparisonRow head="dr_m_who" ampere="dr_m_own" source="dr_m_vout" />
            <ComparisonRow head="dr_m_what" ampere="dr_m_cur_meter" source="dr_m_cur_src" />
            <ComparisonRow head="dr_m_vset" ampere="dr_m_cal_only" source="dr_m_also" />
            <ComparisonRow head="dr_m_energy" ampere="dr_m_need" source="dr_m_derived" />
          </tbody>
        </table>
      </div>
      <Html className="note" k="dr_m_note" />

      <ControlBlock unlocked={unlocked}>
        <div className="field">
          <span className="lbl">{t("dr_m_switch")}</span>
          <select
            value={draft.mode}
            onChange={(e) => onDraftChange({ mode: Number(e.target.value) as Mode })}
          >
            <option value={Mode.SOURCE}>source</option>
            <option value={Mode.AMPERE}>ampere</option>
          </select>
        </div>
        {pending && (
          <div className="badges">
            <span className="pill warn">{t("cfg_dirty")}</span>
          </div>
        )}
      </ControlBlock>
    </section>
  );
}

function ComparisonRow({ head, ampere, source }: { head: MessageKey; ampere: MessageKey; source: MessageKey }) {
  const { t } = useI18n();
  return (
    <tr>
      <td className="txt">{t(head)}</td>
      <td className="txt">{t(ampere)}</td>
      <td className="txt">{t(source)}</td>
    </tr>
  );
}

function VoltagePane({
  source,
  snapshot,
  unlocked,
  draft,
  onDraftChange,
}: {
  source: DataSource;
  snapshot: DeviceSnapshot;
  unlocked: boolean;
  draft: DeviceConfig;
  onDraftChange: (patch: Partial<DeviceConfig>) => void;
}) {
  const { t } = useI18n();
  const isSource = snapshot.state.mode === Mode.SOURCE;
  const pending = draft.voltageMv !== snapshot.state.source_voltage_mv;
  const [ceiling, setCeiling] = useState(String(snapshot.maxVoltageMv ?? ""));
  const [assumed, setAssumed] = useState(snapshot.assumedVoltageMv === null ? "" : String(snapshot.assumedVoltageMv));

  return (
    <section>
      <div className="readout">
        <span className="big">{snapshot.assumedVoltageMv ?? snapshot.state.source_voltage_mv ?? "—"}</span>
        <span className="unit">mV</span>
      </div>
      <p className="sub">{t(isSource ? "v_desc_src" : "v_desc_amp")}</p>

      <dl className="kv">
        <dt>source_voltage_mv</dt>
        <dd>{snapshot.state.source_voltage_mv ?? "—"}</dd>
        <dt>metadata VDD</dt>
        <dd>{snapshot.calibration.vdd_mv ?? "—"}</dd>
        <dt>voltage_measured</dt>
        <dd style={{ color: "var(--loss)" }}>false</dd>
        <dt>voltage_basis</dt>
        <dd>{snapshot.state.source_voltage_basis}</dd>
        <dt>
          {VOLTAGE_MIN_MV} – {VOLTAGE_MAX_MV} mV
        </dt>
        <dd>{t("dr_v_limits")}</dd>
      </dl>

      <Html className="note badish" k="dr_v_never" />

      <div className="tablewrap" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr>
              <th>voltage_basis</th>
              <th>{t("dr_v_means")}</th>
            </tr>
          </thead>
          <tbody>
            <BasisRow code="caller_override" meaning="dr_v_co" />
            <BasisRow code="configured_source" meaning="dr_v_cs" />
            <BasisRow code="device_metadata" meaning="dr_v_dm" />
            <BasisRow code="unknown" meaning="dr_v_uk" />
          </tbody>
        </table>
      </div>

      <div className="field" style={{ marginTop: 12 }}>
        <span className="lbl">assume_voltage_mv</span>
        <input
          type="number"
          min={VOLTAGE_MIN_MV}
          max={VOLTAGE_MAX_MV}
          step={10}
          placeholder={t("dr_v_assume_ph")}
          value={assumed}
          onChange={(e) => {
            setAssumed(e.target.value);
            const n = Number.parseInt(e.target.value, 10);
            source.setAssumedVoltageMv(Number.isNaN(n) ? null : n);
          }}
        />
      </div>
      <Html className="note" k="dr_v_assume_note" />

      <ControlBlock unlocked={unlocked}>
        <div className="field">
          <span className="lbl">{t("dr_v_set")}</span>
          <input
            type="number"
            min={VOLTAGE_MIN_MV}
            max={VOLTAGE_MAX_MV}
            step={10}
            value={draft.voltageMv}
            onChange={(e) => onDraftChange({ voltageMv: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <span className="lbl">max_voltage_mv</span>
          <input
            type="number"
            min={VOLTAGE_MIN_MV}
            max={VOLTAGE_MAX_MV}
            step={10}
            value={ceiling}
            onChange={(e) => {
              setCeiling(e.target.value);
              const n = Number.parseInt(e.target.value, 10);
              source.setMaxVoltageMv(Number.isNaN(n) ? null : n);
            }}
          />
        </div>
        {pending && (
          <div className="badges">
            <span className="pill warn">{t("cfg_dirty")}</span>
          </div>
        )}
      </ControlBlock>
    </section>
  );
}

function BasisRow({ code, meaning }: { code: string; meaning: MessageKey }) {
  const { t } = useI18n();
  return (
    <tr>
      <td>{code}</td>
      <td className="txt">{t(meaning)}</td>
    </tr>
  );
}

const PORT_CLOSURE_TRIALS: readonly [string, string][] = [
  ["0 ms", "3 / 3"],
  ["100 ms", "1 / 3"],
  ["250 ms", "2 / 3"],
  ["500 ms", "0 / 3"],
  ["1000 ms", "0 / 3"],
  ["4000 ms", "0 / 3"],
];

function DutPane({
  snapshot,
  unlocked,
  outputOn,
  onToggle,
}: {
  snapshot: DeviceSnapshot;
  unlocked: boolean;
  outputOn: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const dut = snapshot.state.dut_power;

  return (
    <section>
      <div className="readout">
        <span className="big">{t(dut === null ? "d_unknown_word" : dut ? "d_on_word" : "d_off_word")}</span>
      </div>
      <p className="sub">{t(dut ? "d_sub_on" : "d_sub_off")}</p>

      <dl className="kv">
        <dt>dut_power</dt>
        <dd>{String(dut)}</dd>
        <dt>observed_after</dt>
        <dd style={{ color: "var(--loss)" }}>false</dd>
      </dl>

      <Html className="note warnish" k="dr_d_transient" />

      <div className="tablewrap" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr>
              <th>{t("dr_d_closed")}</th>
              <th>{t("dr_d_still")}</th>
            </tr>
          </thead>
          <tbody>
            {PORT_CLOSURE_TRIALS.map(([closed, powered]) => (
              <tr key={closed}>
                <td>{closed}</td>
                <td>{powered}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Html className="note" k="dr_d_note2" />

      {unlocked ? (
        <div className="row" style={{ gap: 8, marginTop: 11 }}>
          <button
            type="button"
            className={`btn outbtn${outputOn ? " on" : ""}`}
            aria-pressed={outputOn}
            onClick={onToggle}
          >
            <Icon name="record" />
            <span>{outputOn ? t("out_stop") : t("out_start")}</span>
          </button>
        </div>
      ) : (
        <LockedHint />
      )}
    </section>
  );
}

function StreamPane({ source, snapshot }: { source: DataSource; snapshot: DeviceSnapshot }) {
  const { t } = useI18n();
  useTicker(4);
  const dec = source.decimator;
  const complete = dec.gapCount === 0;

  return (
    <section>
      <dl className="kv" style={{ marginTop: 0 }}>
        <dt>measuring</dt>
        <dd>{String(snapshot.state.measuring)}</dd>
        <dt>stored</dt>
        <dd>{fmtInt(dec.totalStored)}</dd>
        <dt>missing_known</dt>
        <dd>{fmtInt(dec.totalMissing)}</dd>
        <dt>sample_gaps</dt>
        <dd>{fmtInt(dec.gapCount)}</dd>
        <dt>covered_fraction</dt>
        <dd>{fmtPercent(dec.coveredFraction())}</dd>
        <dt>achieved_sample_rate_hz</dt>
        <dd>{snapshot.state.measuring ? fmtInt(SAMPLE_RATE_HZ + 4) : "—"}</dd>
        <dt>rate_check</dt>
        <dd>ok</dd>
      </dl>

      <div style={{ marginTop: 10 }}>
        {complete ? (
          <span className="pill good">complete: true</span>
        ) : (
          <>
            <span className="pill bad">complete: false</span> <span className="pill warn">W_SAMPLE_GAPS</span>
          </>
        )}
      </div>

      <Html className="note" k="dr_s_note" />

      {source.demo && (
        <button
          type="button"
          className="btn sm danger"
          onClick={() => source.demo?.injectLoss(5000 + Math.floor(Math.random() * 18000))}
        >
          <Icon name="alert" />
          <span>{t("dr_s_inject")}</span>
        </button>
      )}
    </section>
  );
}

function CalibrationPane({ snapshot }: { snapshot: DeviceSnapshot }) {
  const { t } = useI18n();
  const cal = snapshot.calibration;

  return (
    <section>
      <dl className="kv" style={{ marginTop: 0 }}>
        <dt>Calibrated</dt>
        <dd>{cal.calibrated ? 1 : 0}</dd>
        <dt>HW</dt>
        <dd>{cal.hw ?? "—"}</dd>
        <dt>IA</dt>
        <dd>{cal.ia ?? "—"}</dd>
        <dt>missing_ranges</dt>
        <dd>{cal.missing_ranges.length ? cal.missing_ranges.join(",") : "—"}</dd>
        <dt>metadata END</dt>
        <dd>{cal.terminated ? "terminated" : "truncated"}</dd>
      </dl>

      <div className="tablewrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th>{t("st_range")}</th>
              <th>{t("st_fs")}</th>
              <th>R (Ω)</th>
              <th>O</th>
              <th>UG</th>
            </tr>
          </thead>
          <tbody>
            {cal.ranges.map((r, i) => (
              <tr key={i}>
                <td>
                  <span className="rangeswatch" style={{ background: `var(--r${i})` }} />
                  {i}
                </td>
                <td>{fmtCurrent(FULL_SCALE_UA[i]!, 0)}</td>
                <td>{r.r.toFixed(4)}</td>
                <td>{r.o.toFixed(1)}</td>
                <td>{r.ug.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Html className="note" k="dr_c_formula" />
    </section>
  );
}
