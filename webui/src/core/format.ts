/** Value formatting shared by the panels, the chart axis, and the tooltip. */

/**
 * Format a current in microamps, picking the unit from the magnitude.
 *
 * Never rounds a negative reading to zero: near true zero the calibrated value
 * legitimately falls on both sides of the offset, and hiding that would inflate
 * every sub-microamp mean (`docs/faq.md`, "Why do I see negative current?").
 */
export function fmtCurrent(uA: number | null | undefined, digits = 3): string {
  if (uA === null || uA === undefined || !Number.isFinite(uA)) return "—";
  const a = Math.abs(uA);
  if (a >= 1e6) return `${(uA / 1e6).toFixed(digits)} A`;
  if (a >= 1e3) return `${(uA / 1e3).toFixed(digits)} mA`;
  if (a >= 1) return `${uA.toFixed(digits)} µA`;
  return `${(uA * 1000).toFixed(digits === 3 ? 1 : digits)} nA`;
}

export function fmtInt(n: number): string {
  return n.toLocaleString("en-US");
}

/** Seconds relative to now, signed. Uses a typographic minus, not a hyphen. */
export function fmtRelTime(s: number): string {
  const neg = s < 0;
  const a = Math.abs(s);
  const m = Math.floor(a / 60);
  const sec = a - m * 60;
  const body = m > 0 ? `${m}m${sec < 10 ? "0" : ""}${sec.toFixed(2)}s` : `${sec.toFixed(3)}s`;
  return (neg ? "−" : "+") + body;
}

export function fmtPercent(x: number, digits = 3): string {
  return `${(x * 100).toFixed(digits)}%`;
}
