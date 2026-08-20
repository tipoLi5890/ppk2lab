import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import type { ConsoleEvent, DataSource, DeviceSnapshot } from "./data/source";

/**
 * Re-render when the device state or the event log changes.
 *
 * Deliberately not subscribed to sample data: that changes 100,000 times a
 * second and is read straight from the decimator by the chart loop.
 */
export function useDeviceSnapshot(source: DataSource): DeviceSnapshot {
  return useSyncExternalStore(
    (cb) => source.subscribe(cb),
    () => source.snapshot(),
  );
}

export function useEvents(source: DataSource): readonly ConsoleEvent[] {
  return useSyncExternalStore(
    (cb) => source.subscribe(cb),
    () => source.events(),
  );
}

/**
 * A slow clock for panels showing continuously changing counters.
 *
 * Stored-sample counts and window statistics move every millisecond; they are
 * read four times a second because that is as often as a person can use them.
 */
export function useTicker(hz = 4): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000 / hz);
    return () => window.clearInterval(id);
  }, [hz]);
  return tick;
}

/** Close on Escape, and on a click outside the element. */
export function useDismiss(
  open: boolean,
  onClose: () => void,
  ref: React.RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    if (!open) return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const click = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("keydown", key);
    document.addEventListener("click", click);
    return () => {
      document.removeEventListener("keydown", key);
      document.removeEventListener("click", click);
    };
  }, [open, onClose, ref]);
}

/**
 * True for a moment after `value` changes.
 *
 * For state the reader did not cause — a gap appearing, coverage falling — the
 * badge has to catch the eye once and then stop. Skips the first render, so
 * loading the page does not look like an event.
 */
export function useFlash(value: string, ms = 700): boolean {
  const previous = useRef<string | null>(null);
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (previous.current === null) {
      previous.current = value;
      return;
    }
    if (previous.current === value) return;
    previous.current = value;
    setOn(true);
    const id = window.setTimeout(() => setOn(false), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return on;
}

export type Theme = "light" | "dark" | null;

/** Explicit theme choice, stamped on `<html>`; null means follow the system. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(null);
  useEffect(() => {
    if (theme === null) document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  const toggle = () => {
    setTheme((current) => {
      const dark =
        current === null ? window.matchMedia("(prefers-color-scheme: dark)").matches : current === "dark";
      return dark ? "light" : "dark";
    });
  };
  return [theme, toggle];
}
