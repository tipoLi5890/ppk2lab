import { useRef, useState } from "react";

import type { ConnectionStatus } from "../data/source";
import { useDismiss, useTicker } from "../hooks";
import { LANGUAGES, useI18n } from "../i18n";
import type { DeviceInfo } from "../types";
import { Icon } from "./Icons";

interface RailProps {
  info: DeviceInfo;
  simulated: boolean;
  /** How this console's link to the server stands. Not the device's state. */
  connection: ConnectionStatus;
  connected: boolean;
  streaming: boolean;
  onToggleStream: () => void;
  controlUnlocked: boolean;
  onToggleControl: () => void;
  onToggleTheme: () => void;
  /** VOUT as the host last requested it. Never confirmed by the device. */
  outputOn: boolean;
  onToggleOutput: () => void;
  /** Why energising VOUT would be pointless or unverifiable right now. */
  outputCaveat: string | null;
  /** Fields edited but not yet applied. */
  dirtyCount: number;
  /** A dry run is in flight; the device has not been asked to do anything. */
  staging: boolean;
  onApplyDraft: () => void;
  onRevertDraft: () => void;
}

/**
 * Identity and session controls.
 *
 * Sticky, because stopping the stream and locking control are the two things a
 * reader may want without scrolling back up.
 */
/**
 * Where this console stands with the server.
 *
 * A badge rather than a modal, and deliberately so: reading the history you
 * already have while the link is down is a legitimate thing to do, and the
 * console is a viewer first. The wording never says "stopped" -- the device is
 * very likely still measuring; what stopped is this console's view of it.
 */
function ConnectionPill({ connection }: { connection: ConnectionStatus }) {
  const { t, lang } = useI18n();
  // A countdown at 1 Hz, driven here so nothing else re-renders for it.
  useTicker(connection.phase === "reconnecting" ? 1 : 0);
  if (connection.phase === "simulated") return null;

  const seconds =
    connection.nextAttemptAtMs === null
      ? 0
      : Math.max(0, Math.ceil((connection.nextAttemptAtMs - performance.now()) / 1000));

  const [label, tone, hint] =
    connection.phase === "open"
      ? [t("conn_live"), "good", t("conn_hint_open")]
      : connection.phase === "connecting"
        ? [t("conn_connecting"), "unk", t("conn_hint_closed")]
        : connection.phase === "reconnecting"
          ? [
              t("conn_reconnecting", seconds ? `${seconds}s` : ""),
              "warn",
              t("conn_hint_lost", connection.attempt),
            ]
          : [t("conn_offline"), "bad", t(connection.reasonKey ?? "conn_hint_closed")];

  void lang;
  return (
    <span className={`pill ${tone}`} title={hint.replace(/<[^>]+>/g, "")}>
      {label}
    </span>
  );
}

export function Rail({
  info,
  simulated,
  connection,
  connected,
  streaming,
  onToggleStream,
  controlUnlocked,
  onToggleControl,
  onToggleTheme,
  outputOn,
  onToggleOutput,
  outputCaveat,
  dirtyCount,
  staging,
  onApplyDraft,
  onRevertDraft,
}: RailProps) {
  const { t } = useI18n();

  return (
    <div className="rail">
      <div className="brand">
        <b>PPK2 Bench Console</b>
        {/* The banner is a badge, not a sentence: it has to be unmissable, not
            long. What --simulate means is one hover away and in the docs. */}
        {simulated && (
          <span className="pill warn" title={t("sim_text").replace(/<[^>]+>/g, "")}>
            SIMULATED
          </span>
        )}
        <ConnectionPill connection={connection} />
      </div>

      <div className="idset">
        <IdCell label={t("r_serial")} value={info.serial_number ?? "—"} />
        <IdCell label={t("r_fw")} value={info.firmware_version ?? "—"} />
        <IdCell label={t("r_port")} value={info.measurement_port ?? "—"} />
        <IdCell label={t("r_fp")} value={info.fingerprint ?? "—"} />
      </div>

      <div className="railctl">
        {/* Staged edits commit from here, not from the drawer that made them:
            the draft spans two panels, so the place to resolve it is the one
            place that is always visible. */}
        {controlUnlocked && dirtyCount > 0 && (
          <span className="draftbar">
            <span className="pill warn">
              {dirtyCount} {t("cfg_dirty")}
            </span>
            <button type="button" className="btn sm" onClick={onRevertDraft}>
              {t("cfg_revert")}
            </button>
            <button
              type="button"
              className="btn sm primary"
              disabled={!connected || staging}
              title={connected ? undefined : t("conn_locked")}
              onClick={onApplyDraft}
            >
              {staging ? t("cfg_checking") : t("cfg_apply")}
            </button>
          </span>
        )}

        {/* Arming needs a confirmation; disarming must not, because turning the
            output off is the fail-safe direction and should never be blocked. */}
        {controlUnlocked && (
          <button
            type="button"
            className={`btn outbtn${outputOn ? " on" : ""}`}
            aria-pressed={outputOn}
            disabled={!connected}
            onClick={onToggleOutput}
            title={
              !connected
                ? t("conn_locked")
                : outputOn
                  ? `${t("out_stop")} — ${t("out_requested")}`
                  : [t("out_start"), outputCaveat].filter(Boolean).join(" — ")
            }
          >
            <Icon name="record" />
            <span>
              {t("out_label")} {outputOn ? "ON" : "OFF"}
            </span>
          </button>
        )}

        <button
          type="button"
          className={`btn livebtn${streaming && connected ? " on" : ""}`}
          aria-pressed={streaming}
          disabled={!connected}
          title={connected ? undefined : t("conn_locked")}
          onClick={onToggleStream}
        >
          <Icon key={streaming ? "activity" : "pause"} name={streaming ? "activity" : "pause"} />
          <span>{streaming ? t("r_live_on") : t("r_live_off")}</span>
        </button>

        <button
          type="button"
          className="btn"
          aria-pressed={controlUnlocked}
          title={t("r_lock_title")}
          onClick={onToggleControl}
        >
          <Icon key={controlUnlocked ? "unlock" : "lock"} name={controlUnlocked ? "unlock" : "lock"} />
          <span>{controlUnlocked ? t("r_unlocked") : t("r_locked")}</span>
        </button>

        <LanguageMenu />

        <button
          type="button"
          className="btn icon"
          title={t("r_theme")}
          aria-label={t("r_theme")}
          onClick={onToggleTheme}
        >
          <Icon name="contrast" />
        </button>
      </div>
    </div>
  );
}

function IdCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="idcell">
      <span className="lbl">{label}</span>
      <span className="val">{value}</span>
    </div>
  );
}

/**
 * Globe control with a dropdown.
 *
 * A menu rather than a row of buttons so the number of languages is not a
 * layout constraint: adding one is a catalogue plus a row in `LANGUAGES`.
 */
function LanguageMenu() {
  const { lang, setLang, t } = useI18n();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  useDismiss(open, () => setOpen(false), wrapRef);

  const current = LANGUAGES.find((l) => l.code === lang) ?? LANGUAGES[0]!;

  return (
    <div className="menuwrap" ref={wrapRef}>
      <button
        type="button"
        className="btn"
        id="btn-lang"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`${t("r_lang")} — ${current.name}`}
        title={`${t("r_lang")} — ${current.name}`}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <Icon name="globe" />
        <span id="lang-short">{current.short}</span>
        <Icon name="caret" className="caret" />
      </button>

      {open && (
        <div className="menu" role="menu">
          {LANGUAGES.map((l) => (
            <button
              key={l.code}
              type="button"
              role="menuitemradio"
              aria-checked={l.code === lang}
              onClick={() => {
                setLang(l.code);
                setOpen(false);
              }}
            >
              <Icon name="check" className="tick" />
              <span>{l.name}</span>
              <span className="code">{l.code}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
