import { useRef, useState } from "react";

import { useDismiss } from "../hooks";
import { LANGUAGES, useI18n } from "../i18n";
import type { DeviceInfo } from "../types";
import { Icon } from "./Icons";

interface RailProps {
  info: DeviceInfo;
  simulated: boolean;
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
  onApplyDraft: () => void;
  onRevertDraft: () => void;
}

/**
 * Identity and session controls.
 *
 * Sticky, because stopping the stream and locking control are the two things a
 * reader may want without scrolling back up.
 */
export function Rail({
  info,
  simulated,
  streaming,
  onToggleStream,
  controlUnlocked,
  onToggleControl,
  onToggleTheme,
  outputOn,
  onToggleOutput,
  outputCaveat,
  dirtyCount,
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
            <button type="button" className="btn sm primary" onClick={onApplyDraft}>
              {t("cfg_apply")}
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
            onClick={onToggleOutput}
            title={
              outputOn
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
          className={`btn livebtn${streaming ? " on" : ""}`}
          aria-pressed={streaming}
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
