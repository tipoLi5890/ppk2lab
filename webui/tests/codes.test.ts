import { describe, expect, it } from "vitest";

import {
  ERROR_KEYS,
  WARNING_KEYS,
  errorMessage,
  rejectionMessage,
  warningMessage,
} from "../src/data/codes";
import { en } from "../src/i18n/en";
import { translate } from "../src/i18n";

/**
 * `translate` falls back `dict[key] ?? en[key] ?? key`, so a key that does not
 * exist renders as its own name. That is the failure these tests exist to
 * catch: it is silent, it looks like a label, and it shipped.
 */
const rendersAsItsOwnKey = (key: string) => translate("en", key as never) === key;

describe("warning and error codes", () => {
  it("maps every table entry to a key the catalogue defines", () => {
    for (const [code, key] of Object.entries(WARNING_KEYS)) {
      expect(key in en, `${code} -> ${key}`).toBe(true);
      expect(rendersAsItsOwnKey(key), `${key} renders as its own name`).toBe(false);
    }
    for (const [code, key] of Object.entries(ERROR_KEYS)) {
      expect(key in en, `${code} -> ${key}`).toBe(true);
      expect(rendersAsItsOwnKey(key)).toBe(false);
    }
  });

  it("renders W_DUT_POWER_TRANSIENT as a sentence, not as a key", () => {
    // The regression: `w_${code.slice(2).toLowerCase()}` produced
    // `w_dut_power_transient`, but the catalogue spells it `w_dut_transient`,
    // so arming VOUT logged the literal key into the event log.
    const { key, args } = warningMessage("W_DUT_POWER_TRANSIENT");
    expect(key).toBe("w_dut_transient");
    expect(translate("en", key, ...args)).toContain("VOUT");
  });

  it("treats an unknown warning code as unknown, not as invalid", () => {
    // diagnostics.py publishes its catalogue as open: a reader must carry a
    // code it does not know rather than fail closed or drop it.
    const { key, args } = warningMessage("W_TIMELINE_COMPRESSION", "the timeline was compressed");
    expect(key).toBe("w_unknown");
    const rendered = translate("en", key, ...args);
    expect(rendered).toContain("W_TIMELINE_COMPRESSION");
    expect(rendered).toContain("the timeline was compressed");
  });

  it("never returns a key that would render as its own name", () => {
    // Every code diagnostics.py can emit, translated or not, has to come back
    // as something a person can read.
    const codes = [
      "W_SAMPLE_GAPS",
      "W_DUT_POWER_TRANSIENT",
      "W_STATE_UNVERIFIED",
      "W_DRY_RUN",
      "W_SESSION_RECOVERED",
      "W_NOT_CALIBRATED",
      "W_BELOW_MEASUREMENT_FLOOR",
      "W_SOMETHING_ADDED_AFTER_THIS_RELEASE",
    ];
    for (const code of codes) {
      const { key } = warningMessage(code, "a sentence from the source");
      expect(rendersAsItsOwnKey(key), `${code} -> ${key}`).toBe(false);
    }
  });

  it("prefers a detail over the class when one code covers two cases", () => {
    // VoltageRangeError raises VOLTAGE_OUT_OF_RANGE for both the device limits
    // and the session ceiling, and only the ceiling message carries the
    // actionable remediation.
    expect(errorMessage("VOLTAGE_OUT_OF_RANGE").key).toBe("er_range");
    expect(errorMessage("VOLTAGE_OUT_OF_RANGE", [4200, 3600], "SESSION_CEILING").key).toBe(
      "er_ceiling",
    );
  });

  it("renders a rejection's own key rather than passing it through the raw table", () => {
    // `ControlRejected.code` is already a message key: the server picks it, and
    // the simulated source raises one directly. Feeding it to `errorMessage` --
    // which maps *raw* codes -- finds nothing and falls through to
    // `er_unknown`, printing the key as the visible first argument. That was
    // exactly the bare-identifier-where-a-sentence-belongs failure this module
    // was written to end, reintroduced at the call site.
    for (const code of [
      "er_range",
      "er_ceiling",
      "er_locked",
      "er_held",
      "er_offline",
      "er_busy",
      "er_timeout",
      "er_disconnected",
      "er_state_moved",
      "er_recording",
    ]) {
      const { key, args } = rejectionMessage(code, []);
      expect(key, code).toBe(code);
      const rendered = translate("en", key, ...args);
      expect(rendered, code).not.toBe(code);
      expect(rendered, code).not.toContain(code);
      // And the raw-code table would have got it wrong, which is the point.
      expect(errorMessage(code, []).key).toBe("er_unknown");
    }
  });

  it("does not invent a key for a rejection the catalogue never defined", () => {
    const { key, args } = rejectionMessage("er_something_added_later", ["detail"]);
    expect(key).toBe("er_unknown");
    expect(translate("en", key, ...args)).toContain("er_something_added_later");
  });

  it("falls back to a readable sentence for an unmapped rejection", () => {
    const { key, args } = errorMessage("PORT_BUSY", ["another process holds the port"]);
    expect(key).toBe("er_unknown");
    expect(translate("en", key, ...args)).toContain("PORT_BUSY");
  });
});
