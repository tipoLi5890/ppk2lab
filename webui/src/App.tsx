import { useCallback, useMemo, useRef, useState } from "react";

import type { WindowStats } from "./chart/columns";
import { Chart } from "./components/Chart";
import { Drawer } from "./components/Drawer";
import { IconSprite } from "./components/Icons";
import { Inspector } from "./components/Inspector";
import { Rail } from "./components/Rail";
import { StatusBar, type DrawerPane } from "./components/StatusBar";
import { rejectionMessage } from "./data/codes";
import { getSource } from "./data/select";
import { ApplyDialog } from "./components/ApplyDialog";
import {
  ControlRejected,
  type ApplyPlan,
  type ConfigDiff,
  type DataSource,
  type DeviceConfig,
} from "./data/source";
import { useDeviceSnapshot, useEvents, useTheme } from "./hooks";
import { I18nContext, detectLanguage, translate, type MessageKey } from "./i18n";
import { Mode } from "./types";

/**
 * Pick the source once per tab.
 *
 * Memoised at module scope inside `select.ts` rather than here: React Fast
 * Refresh remounts this component on every edit to it, and a fresh source
 * means a fresh socket against a server whose whole premise is that one
 * process owns the port.
 */
function createSource(): DataSource {
  return getSource();
}

export default function App() {
  const sourceRef = useRef<DataSource | null>(null);
  sourceRef.current ??= createSource();
  const source = sourceRef.current;

  const [lang, setLang] = useState(detectLanguage);
  const i18n = useMemo(
    () => ({
      lang,
      setLang,
      t: (key: MessageKey, ...args: (string | number)[]) => translate(lang, key, ...args),
    }),
    [lang],
  );
  if (document.documentElement.lang !== lang) document.documentElement.lang = lang;

  const snapshot = useDeviceSnapshot(source);
  const events = useEvents(source);
  const [, toggleTheme] = useTheme();

  const [controlUnlocked, setControlUnlocked] = useState(false);
  const [drawer, setDrawer] = useState<DrawerPane | null>(null);
  const [plan, setPlan] = useState<ApplyPlan | null>(null);
  const [rejection, setRejection] = useState<string | null>(null);

  // Edits are staged rather than sent: the operator sees everything that will
  // change, and in what order, before any of it reaches the wire.
  const applied = source.config();
  const [draft, setDraft] = useState<DeviceConfig>(applied);
  const patchDraft = useCallback((patch: Partial<DeviceConfig>) => {
    setDraft((d) => ({ ...d, ...patch }));
  }, []);

  // Follow the device when nothing is staged.
  //
  // `useState(applied)` reads the config once, at mount. The simulated source
  // knows its config in its constructor, so draft and applied agree forever
  // and this never mattered. A source that learns the config from a device --
  // milliseconds after mount, and again whenever the device moves under us --
  // would otherwise leave the rail showing edits the operator never made, next
  // to a live Apply button that commands hardware.
  //
  // When something *is* staged the diff is truthful: the device moved while
  // the operator was mid-edit, and that is exactly what they need to see.
  const [baseline, setBaseline] = useState<DeviceConfig>(applied);
  if (baseline.mode !== applied.mode || baseline.voltageMv !== applied.voltageMv) {
    const untouched = draft.mode === baseline.mode && draft.voltageMv === baseline.voltageMv;
    setBaseline(applied);
    if (untouched) setDraft(applied);
  }

  const diff: ConfigDiff[] = [];
  if (draft.mode !== applied.mode) {
    diff.push({
      field: "mode",
      from: applied.mode === Mode.SOURCE ? "source" : "ampere",
      to: draft.mode === Mode.SOURCE ? "source" : "ampere",
    });
  }
  if (draft.voltageMv !== applied.voltageMv) {
    diff.push({
      field: "source_voltage_mv",
      from: String(applied.voltageMv),
      to: String(draft.voltageMv),
    });
  }
  const outputOn = source.outputOn();

  const statsReaderRef = useRef<(() => WindowStats | null) | null>(null);
  const handleHandles = useCallback((readStats: () => WindowStats | null) => {
    statsReaderRef.current = readStats;
  }, []);
  const readStats = useCallback(() => statsReaderRef.current?.() ?? null, []);

  const reject = useCallback(
    (err: unknown) => {
      // Everything reaches the operator. This runs as a `.catch` handler, so
      // re-throwing here produced an unhandled rejection and nothing else:
      // no error boundary, no console entry the operator would see, and a
      // state change that silently did not happen. Silence is worse than a
      // crash for a control surface.
      if (err instanceof ControlRejected) {
        // `code` is already a message key -- not a raw library code -- so it is
        // checked against the catalogue rather than mapped through the
        // raw-code table, which would find nothing and print the key.
        const { key, args } = rejectionMessage(err.code, err.args);
        setRejection(translate(lang, key, ...args));
        return;
      }
      const detail = err instanceof Error ? err.message : String(err);
      setRejection(translate(lang, "er_unknown", "UNEXPECTED", detail));
    },
    [lang],
  );

  const [staging, setStaging] = useState(false);

  /**
   * Stage the draft for confirmation.
   *
   * The dialog says "no byte has reached the wire", and now that is a fact
   * about the device rather than about this component: the plan is sent as a
   * dry run first, and what the dialog shows is the before and after the
   * device itself projected, with the warnings it raised. Nothing is applied.
   */
  const proposeDraft = useCallback(() => {
    if (diff.length === 0 || staging) return;
    const staged: ApplyPlan = {
      changes: diff.map((d) =>
        d.field === "mode"
          ? { kind: "mode" as const, mode: draft.mode }
          : { kind: "voltage" as const, voltageMv: draft.voltageMv },
      ),
      diff,
      // A live output is dropped before the settings move, never during.
      stopOutputFirst: outputOn,
      restartOutput: false,
      interruptsStream: true,
    };
    setStaging(true);
    source
      .previewPlan(staged)
      .then((preview) =>
        setPlan({ ...staged, interruptsStream: preview.interruptsStream, preview }),
      )
      .catch(reject)
      .finally(() => setStaging(false));
  }, [diff, draft, outputOn, source, reject, staging]);

  /**
   * Arming asks; disarming does not.
   *
   * Turning the output off is the fail-safe direction and must never be behind
   * a dialog — the moment someone wants VOUT down is the moment they should get
   * it. Turning it on energises a DUT, so it goes through the same gate as any
   * other state change.
   */
  const toggleOutput = useCallback(() => {
    if (outputOn) {
      source.apply({ kind: "dut-power", on: false }).catch(reject);
      return;
    }
    const staged: ApplyPlan = {
      changes: [{ kind: "dut-power", on: true }],
      diff: [{ field: "dut_power", from: "false", to: "true" }],
      stopOutputFirst: false,
      restartOutput: false,
      interruptsStream: false,
    };
    setStaging(true);
    source
      .previewPlan(staged)
      .then((preview) => setPlan({ ...staged, preview }))
      .catch(reject)
      .finally(() => setStaging(false));
  }, [outputOn, source, reject]);

  const commitPlan = useCallback(
    (restartOutput: boolean) => {
      const current = plan;
      if (!current) return;
      setPlan(null);
      source.applyPlan({ ...current, restartOutput }).catch(reject);
    },
    [plan, source, reject],
  );

    const connection = snapshot.connection;
  // Connected means this console can see the device. It is not the same as the
  // device measuring, and the difference is exactly what an operator needs
  // when a link drops: the trace freezes, and the instrument does not.
  const connected = connection.phase === "simulated" || connection.phase === "open";
  const streaming = snapshot.state.measuring;

  // Why energising VOUT right now would be pointless, or impossible to verify.
  const outputCaveat = !streaming
    ? i18n.t("out_noverify")
    : snapshot.state.mode === Mode.AMPERE
      ? i18n.t("out_ampere")
      : null;

  return (
    <I18nContext.Provider value={i18n}>
      <IconSprite />

      {/* One viewport, four rows: identity, state, trace, inspector. The trace
          is the only row that stretches — everything else is the size it needs. */}
      <div className="app">
        <Rail
          info={snapshot.info}
          simulated={source.simulated}
          connection={connection}
          connected={connected}
          streaming={streaming}
          onToggleStream={() =>
            void (streaming ? source.stopStream() : source.startStream()).catch(reject)
          }
          controlUnlocked={controlUnlocked}
          onToggleControl={() => setControlUnlocked((v) => !v)}
          onToggleTheme={toggleTheme}
          outputOn={outputOn}
          onToggleOutput={toggleOutput}
          outputCaveat={outputCaveat}
          dirtyCount={diff.length}
          staging={staging}
          onApplyDraft={proposeDraft}
          onRevertDraft={() => setDraft(applied)}
        />

        <StatusBar source={source} snapshot={snapshot} onOpen={setDrawer} />

        <Chart source={source} onHandles={handleHandles} />

        <Inspector
          source={source}
          snapshot={snapshot}
          events={events}
          readStats={readStats}
          controlUnlocked={controlUnlocked}
        />
      </div>

      <Drawer
        pane={drawer}
        onClose={() => setDrawer(null)}
        source={source}
        snapshot={snapshot}
        controlUnlocked={controlUnlocked}
        draft={draft}
        onDraftChange={patchDraft}
        outputOn={outputOn}
        onToggleOutput={toggleOutput}
      />

      <ApplyDialog plan={plan} onCancel={() => setPlan(null)} onApply={commitPlan} />

      {rejection && <Rejection text={rejection} onDismiss={() => setRejection(null)} />}
    </I18nContext.Provider>
  );
}

/** A refused control operation: the reason, in the reader's language. */
function Rejection({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="scrim on" onClick={onDismiss}>
      <div className="modal" role="alertdialog" aria-modal="true" style={{ maxWidth: 460 }}>
        <div className="mbody">
          <p className="note badish" style={{ whiteSpace: "pre-wrap" }}>
            {text}
          </p>
        </div>
        <footer>
          <button type="button" className="btn primary" onClick={onDismiss}>
            OK
          </button>
        </footer>
      </div>
    </div>
  );
}
