import { useState } from "react";

import type { WindowStats } from "../chart/columns";
import type { ConsoleEvent, DataSource, DeviceSnapshot } from "../data/source";
import { useI18n, type MessageKey } from "../i18n";
import { EventsPanel, LatestEvent } from "./EventLog";
import { Icon } from "./Icons";
import {
  CapabilitiesPanel,
  DecoderPanel,
  RecordPanel,
  StatsPanel,
  TriggerPanel,
} from "./panels";

type PanelKey = "stats" | "events" | "trig" | "dec" | "rec" | "caps";

const PANELS: { key: PanelKey; label: MessageKey }[] = [
  { key: "stats", label: "tb_stats" },
  { key: "events", label: "e_title" },
  { key: "trig", label: "tb_trig" },
  { key: "dec", label: "tb_dec" },
  { key: "rec", label: "tb_rec" },
  { key: "caps", label: "tb_caps" },
];

interface InspectorProps {
  source: DataSource;
  snapshot: DeviceSnapshot;
  events: readonly ConsoleEvent[];
  readStats: () => WindowStats | null;
  controlUnlocked: boolean;
}

/**
 * Analysis, folded away.
 *
 * Collapsed it is one bar: the panel names, the most recent event, and a
 * toggle. The trace keeps the rest of the window. Opening a panel takes height
 * from the chart rather than pushing the page into a scroll, so the console
 * stays one screen whatever is open.
 */
export function Inspector({ source, snapshot, events, readStats, controlUnlocked }: InspectorProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState<PanelKey | null>(null);

  const toggle = (key: PanelKey) => setOpen((current) => (current === key ? null : key));

  return (
    <section className={`inspector${open ? " open" : ""}`}>
      <div className="inspectorbar">
        <div className="tabstrip" role="tablist">
          {PANELS.map((p) => (
            <button
              key={p.key}
              type="button"
              role="tab"
              aria-selected={open === p.key}
              aria-expanded={open === p.key}
              onClick={() => toggle(p.key)}
            >
              {t(p.label)}
              {p.key === "events" && <span className="count">{events.length}</span>}
            </button>
          ))}
        </div>

        {!open && <LatestEvent events={events} />}

        <button
          type="button"
          className="btn sm collapse"
          aria-expanded={open !== null}
          aria-label={t(open ? "in_collapse" : "in_expand")}
          title={t(open ? "in_collapse" : "in_expand")}
          onClick={() => setOpen((current) => (current ? null : "stats"))}
        >
          <Icon name="caret" className={open ? "caret up" : "caret"} />
        </button>
      </div>

      {open && (
        <div className="inspectorbody" role="tabpanel">
          {open === "stats" && <StatsPanel source={source} snapshot={snapshot} readStats={readStats} />}
          {open === "events" && <EventsPanel events={events} />}
          {open === "trig" && <TriggerPanel />}
          {open === "dec" && <DecoderPanel />}
          {open === "rec" && <RecordPanel unlocked={controlUnlocked} source={source} />}
          {open === "caps" && <CapabilitiesPanel />}
        </div>
      )}
    </section>
  );
}
