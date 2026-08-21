import type { MessageKey } from "../i18n";
import { en } from "../i18n/en";

/**
 * Warning and error codes, mapped to message keys by an explicit table.
 *
 * The console used to derive the key from the code by string surgery
 * (`` `w_${code.slice(2).toLowerCase()}` `` with an `as MessageKey` cast). That
 * defeated the one guarantee the four `Record<MessageKey, string>` catalogues
 * were built to give — a key that does not exist is a build error — and it is
 * why `W_DUT_POWER_TRANSIENT` shipped rendering as the literal string
 * `w_dut_power_transient`: the catalogue spells it `w_dut_transient`.
 *
 * Here the value type is `MessageKey`, so a typo or a renamed key fails
 * `tsc`. The cost is that a new code has to be added deliberately, which is
 * the point: `diagnostics.py` defines about thirty-five `W_*` codes and the
 * catalogue translates a handful of them.
 */
export const WARNING_KEYS: Readonly<Record<string, MessageKey>> = {
  W_DUT_POWER_TRANSIENT: "w_dut_transient",
  W_STATE_UNVERIFIED: "w_state_unverified",
  W_SAMPLE_GAPS: "w_sample_gaps",
  W_VOLTAGE_ASSUMED: "w_voltage_assumed",
};

/**
 * Rejection codes the control path can raise, mapped the same way.
 *
 * `App`'s reject handler translates `ControlRejected.code` directly, so every
 * code that can reach it has to be a real key or the operator reads an
 * identifier where a sentence belongs.
 */
export const ERROR_KEYS: Readonly<Record<string, MessageKey>> = {
  VOLTAGE_OUT_OF_RANGE: "er_range",
  SESSION_CEILING: "er_ceiling",
};

export interface Translatable {
  key: MessageKey;
  args: (string | number)[];
}

/**
 * A warning code and the sentence its source already wrote, as something the
 * console can render.
 *
 * An unrecognised code is unknown, not invalid — `diagnostics.py` publishes
 * its catalogue as open, and `docs/SPEC.md` says a reader must treat a code it
 * does not know as unknown rather than failing closed. So an untranslated code
 * falls back to `w_unknown`, which shows the code beside the message its
 * source supplied, rather than being dropped or rendered as a bare key.
 */
export function warningMessage(code: string, message = ""): Translatable {
  const key = WARNING_KEYS[code];
  if (key) return { key, args: [] };
  return { key: "w_unknown", args: [code, message] };
}

/**
 * What to show for a `ControlRejected`.
 *
 * Its `code` is a message key by contract -- both sources raise one, and the
 * server picks it from `ppk2lab_web/codes.py`. This verifies rather than
 * trusts: a key the catalogue does not define would otherwise render as its own
 * name, which is the bare-identifier-where-a-sentence-belongs failure the
 * explicit tables above exist to prevent.
 *
 * Deliberately NOT `errorMessage`, which maps *raw* library codes like
 * `VOLTAGE_OUT_OF_RANGE`. Passing an already-resolved key through that table
 * finds nothing and falls through to `er_unknown`, printing the key.
 */
export function rejectionMessage(code: string, args: (string | number)[] = []): Translatable {
  if (code in en) return { key: code as MessageKey, args };
  return { key: "er_unknown", args: [code, ...args] };
}

/**
 * What to show for a *raw* code the library produced -- `PORT_BUSY`,
 * `VOLTAGE_OUT_OF_RANGE`. `detail` separates two refusals that share a class.
 *
 * For a code that is already a message key, use `rejectionMessage`.
 */
export function errorMessage(
  code: string,
  args: (string | number)[] = [],
  detail?: string,
): Translatable {
  const key = (detail && ERROR_KEYS[detail]) || ERROR_KEYS[code];
  if (key) return { key, args };
  return { key: "er_unknown", args: [code, ...args] };
}
