/**
 * One inline SVG sprite, referenced by `<use>`.
 *
 * Icons are stroked with `currentColor`, so they inherit their surroundings and
 * need no theme handling. Status badges take a different route — a CSS mask in
 * `styles.css` — so that every `.pill` gets its icon without a call site.
 */
export type IconName =
  | "activity" | "pause" | "lock" | "unlock" | "contrast"
  | "record" | "arrow" | "alert" | "close" | "globe" | "caret" | "check";

export function IconSprite() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true" focusable="false">
      <defs>
        <symbol id="i-activity" viewBox="0 0 24 24"><path d="M2 12h3.8l3-8.4 4.4 16.8 3-8.4H22" /></symbol>
        <symbol id="i-pause" viewBox="0 0 24 24">
          <rect x="6.5" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none" />
          <rect x="13.5" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none" />
        </symbol>
        <symbol id="i-lock" viewBox="0 0 24 24">
          <rect x="4.2" y="10.2" width="15.6" height="10.6" rx="2.2" />
          <path d="M8 10.2V6.8a4 4 0 0 1 8 0v3.4" />
        </symbol>
        <symbol id="i-unlock" viewBox="0 0 24 24">
          <rect x="4.2" y="10.2" width="15.6" height="10.6" rx="2.2" />
          <path d="M8 10.2V6.8a4 4 0 0 1 7.3-2.3" />
        </symbol>
        <symbol id="i-contrast" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="8.6" />
          <path d="M12 3.4a8.6 8.6 0 0 0 0 17.2z" fill="currentColor" stroke="none" />
        </symbol>
        <symbol id="i-record" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6.4" fill="currentColor" stroke="none" /></symbol>
        <symbol id="i-arrow" viewBox="0 0 24 24"><path d="M4.5 12h14" /><path d="M13.4 6.6 18.8 12l-5.4 5.4" /></symbol>
        <symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 3.2 1.6 21.4h20.8z" /><path d="M12 9.4v5.1" /><path d="M12 18.2h.01" /></symbol>
        <symbol id="i-close" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></symbol>
        <symbol id="i-globe" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" /><path d="M3.2 12h17.6" />
          <ellipse cx="12" cy="12" rx="4.2" ry="9" />
        </symbol>
        <symbol id="i-caret" viewBox="0 0 24 24"><path d="M6.5 9.5 12 15 17.5 9.5" /></symbol>
        <symbol id="i-check" viewBox="0 0 24 24"><path d="M4.5 12.4 9.6 17.5 19.5 6.5" /></symbol>
      </defs>
    </svg>
  );
}

export function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return (
    <svg className={`ico ${className}`.trim()} aria-hidden="true">
      <use href={`#i-${name}`} />
    </svg>
  );
}
