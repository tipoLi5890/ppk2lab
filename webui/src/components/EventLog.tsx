import { useState } from "react";

import { fmtRelTime } from "../core/format";
import type { ConsoleEvent, EventKind } from "../data/source";
import { useI18n } from "../i18n";
import { Icon } from "./Icons";

type Filter = "all" | EventKind;

const FILTERS: { key: Filter; label: "e_all" | "e_state" | "e_warn" | "e_loss" }[] = [
  { key: "all", label: "e_all" },
  { key: "state", label: "e_state" },
  { key: "warn", label: "e_warn" },
  { key: "loss", label: "e_loss" },
];

/**
 * Every state change and every loss, in the order they happened.
 *
 * Entries store a message key and its arguments rather than a finished
 * sentence, so switching language re-renders the whole history instead of
 * leaving it in the language it was recorded in.
 */
export function EventsPanel({ events }: { events: readonly ConsoleEvent[] }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState<Filter>("all");

  const rows = events
    .filter((e) => filter === "all" || e.kind === filter)
    .slice()
    .reverse()
    .slice(0, 120);

  return (
    <div className="eventspanel">
      <div className="filterbar">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className="btn sm"
            aria-pressed={filter === f.key}
            onClick={() => setFilter(f.key)}
          >
            {t(f.label)}
          </button>
        ))}
      </div>

      <div className="logscroll">
        {rows.length === 0 ? (
          <div className="ev k-info">
            <div className="msg" style={{ color: "var(--ink-3)" }}>
              {t("e_empty")}
            </div>
          </div>
        ) : (
          rows.map((e) => <Entry key={e.id} event={e} />)
        )}
      </div>
    </div>
  );
}

/**
 * The most recent entry, for the collapsed inspector bar.
 *
 * The console is a viewer first, and a state change or a sample loss is
 * something the reader should not have to open a panel to notice. One line is
 * enough to say that something happened and what it was.
 */
export function LatestEvent({ events }: { events: readonly ConsoleEvent[] }) {
  const { t } = useI18n();
  const latest = events[events.length - 1];
  if (!latest) return null;
  const args =
    latest.messageKey === "ev_applied" && latest.observed !== undefined
      ? [
          String(latest.observed),
          latest.observed ? "var(--good)" : "var(--loss)",
          t(latest.observed ? "ev_obs_yes" : "ev_obs_no"),
        ]
      : latest.args;
  const text = t(latest.messageKey, ...args).replace(/<[^>]+>/g, "");
  return (
    // Keyed on the event so a new one remounts and plays its entrance rather
    // than silently swapping the text under the reader.
    <span key={latest.id} className={`latest k-${latest.kind}`} title={text}>
      <span className="op">{latest.operation}</span>
      <span className="msg">{text}</span>
    </span>
  );
}

function Entry({ event }: { event: ConsoleEvent }) {
  const { t } = useI18n();

  // The one message whose arguments depend on a fact rather than on text.
  const args =
    event.messageKey === "ev_applied" && event.observed !== undefined
      ? [
          String(event.observed),
          event.observed ? "var(--good)" : "var(--loss)",
          t(event.observed ? "ev_obs_yes" : "ev_obs_no"),
        ]
      : event.args;

  // Catalogue strings carry markup (<code>, <b>) and are authored in this
  // repository; nothing user-supplied reaches here.
  const html = t(event.messageKey, ...args);

  return (
    <div className={`ev k-${event.kind}`}>
      <div className="head">
        <span className="op">{event.operation}</span>
        <span className="ts">{fmtRelTime(event.t)}</span>
      </div>
      <div className="msg" dangerouslySetInnerHTML={{ __html: html }} />
      {event.delta && (
        <div className="delta">
          {Object.entries(event.delta).map(([k, v]) => (
            <Fragment key={k} field={k} value={v} />
          ))}
        </div>
      )}
    </div>
  );
}

function Fragment({ field, value }: { field: string; value: { from: string; to: string } | string }) {
  return (
    <>
      <b>{field}</b>
      <span>
        {typeof value === "string" ? (
          value
        ) : (
          <>
            {value.from}
            <Icon name="arrow" className="xs" />
            {value.to}
          </>
        )}
      </span>
    </>
  );
}
