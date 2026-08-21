import type { MessageKey } from "../i18n";
import { Mode } from "../types";

/**
 * How to render the measurement mode, including when it is not known.
 *
 * `DeviceState.mode` is `Mode | null`, and the `null` means unknown — never a
 * default. The console used to collapse it with `mode === Mode.SOURCE`, so an
 * unknown mode rendered as a confident "Ampere Meter". That is not a cosmetic
 * slip: the mode decides whether energy is computable at all (in Ampere mode
 * the PPK2 does not supply the DUT and `energy_uj` is `null`), so a guess here
 * propagates into an energy story with nothing behind it. The DUT-power cell
 * beside it already gets this right and shows UNKNOWN; this makes mode match.
 */
export interface ModeLabels {
  known: boolean;
  /** Untranslated, like the other code-like badges on the status bar. */
  badge: string;
  name: MessageKey;
  hint: MessageKey;
  desc: MessageKey;
  /** The status bar's own tone vocabulary; `unk` is what DUT power uses. */
  tone: "acc" | "unk";
}

const SOURCE: ModeLabels = {
  known: true,
  badge: "SOURCE",
  name: "m_source",
  hint: "m_source_hint",
  desc: "m_source_desc",
  tone: "acc",
};

const AMPERE: ModeLabels = {
  known: true,
  badge: "AMPERE",
  name: "m_ampere",
  hint: "m_ampere_hint",
  desc: "m_ampere_desc",
  tone: "acc",
};

const UNKNOWN: ModeLabels = {
  known: false,
  badge: "UNKNOWN",
  name: "m_unknown",
  hint: "m_unknown_hint",
  desc: "m_unknown_hint",
  tone: "unk",
};

export function modeLabels(mode: Mode | null): ModeLabels {
  if (mode === Mode.SOURCE) return SOURCE;
  if (mode === Mode.AMPERE) return AMPERE;
  return UNKNOWN;
}
