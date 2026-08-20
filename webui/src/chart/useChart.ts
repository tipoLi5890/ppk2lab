import { useCallback, useEffect, useRef, useState } from "react";

import { SAMPLE_RATE_HZ } from "../core/constants";
import type { DataSource } from "../data/source";
import { ColumnAggregate, windowStats, type WindowStats } from "./columns";
import { drawChart, drawCrosshair, readPalette, type ChartLabels, type Palette } from "./draw";
import {
  FULL_RANGE,
  GUTTER,
  MIN_SPAN_S,
  PAD_RIGHT,
  chartLayout,
  chooseTier,
  currentAtY,
  fitRange,
  zoomRange,
  zoomSpan,
  type ChartLayout,
  type CurrentRange,
  type YScale,
} from "./geometry";

export type YMode = "full" | "fit" | "manual";

export interface ChartView {
  /** Width of the time window, in seconds. */
  spanSeconds: number;
  /** Right edge in session seconds; `null` follows the live edge. */
  endSeconds: number | null;
  /** Manual tier index, or -1 for automatic. */
  tier: number;
  scale: YScale;
  showDigital: boolean;
  yMode: YMode;
  /** Only read when `yMode` is `"manual"`. */
  yRange: CurrentRange;
}

export const DEFAULT_VIEW: ChartView = {
  // 1 s, not 5 s: at coarser windows a burst and a sleep floor land in the same
  // column and the waveform stops being readable at a glance.
  spanSeconds: 1,
  endSeconds: null,
  tier: -1,
  scale: "log",
  showDigital: true,
  yMode: "full",
  yRange: FULL_RANGE,
};

/** Redraw at 30fps rather than per frame: a full repaint is ~13k canvas calls. */
const DRAW_INTERVAL_MS = 33;

export interface ChartHandles {
  wrapRef: React.RefObject<HTMLDivElement | null>;
  canvasRef: React.RefObject<HTMLCanvasElement | null>;
  overlayRef: React.RefObject<HTMLCanvasElement | null>;
  hoverColumn: number | null;
  columnsRef: React.RefObject<ColumnAggregate | null>;
  layoutRef: React.RefObject<ChartLayout | null>;
  tierUsed: number;
  bucketCount: number;
  /** Current axis window actually drawn, after fit or zoom. */
  rangeUsed: CurrentRange;
  /** True while the pointer is dragging the timeline. */
  panning: boolean;
  readStats: () => WindowStats | null;
}

/**
 * Drive the chart from outside React.
 *
 * Two things stay out of the render cycle. The sample stream, because 100,000
 * values a second cannot become 100,000 renders. And the pan gesture, because a
 * drag produces a pointer event per frame and the view has to follow it without
 * the tree re-rendering under the pointer: the gesture writes to a ref, and one
 * state update per frame keeps the toolbar honest.
 */
export function useChart(
  source: DataSource,
  view: ChartView,
  onViewChange: (patch: Partial<ChartView>) => void,
  labels: ChartLabels,
  channelRoles: readonly string[],
): ChartHandles {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);

  const columnsRef = useRef<ColumnAggregate | null>(null);
  const layoutRef = useRef<ChartLayout | null>(null);
  const plotWidthRef = useRef(900);
  const paletteRef = useRef<Palette | null>(null);
  const hoverRef = useRef<number | null>(null);
  const viewRef = useRef(view);
  const labelsRef = useRef(labels);
  const rolesRef = useRef(channelRoles);
  const rangeRef = useRef<CurrentRange>(FULL_RANGE);
  const onViewChangeRef = useRef(onViewChange);

  const [hoverColumn, setHoverColumn] = useState<number | null>(null);
  const [tierUsed, setTierUsed] = useState(0);
  const [bucketCount, setBucketCount] = useState(0);
  const [rangeUsed, setRangeUsed] = useState<CurrentRange>(FULL_RANGE);
  const [panning, setPanning] = useState(false);

  viewRef.current = view;
  labelsRef.current = labels;
  rolesRef.current = channelRoles;
  onViewChangeRef.current = onViewChange;

  const readStats = useCallback((): WindowStats | null => {
    const cols = columnsRef.current;
    return cols ? windowStats(cols, SAMPLE_RATE_HZ) : null;
  }, []);

  /** Right edge actually drawn: the live edge unless the reader panned away. */
  const effectiveEnd = useCallback((): number => {
    const v = viewRef.current;
    const now = source.now();
    if (v.endSeconds === null) return now;
    const oldest = source.historyStart() + v.spanSeconds;
    return Math.min(now, Math.max(oldest, v.endSeconds));
  }, [source]);

  const resize = useCallback(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    const overlay = overlayRef.current;
    if (!wrap || !canvas || !overlay) return;

    const style = getComputedStyle(wrap);
    const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const padY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const cssWidth = Math.max(320, wrap.clientWidth - padX);
    const available = Math.max(120, wrap.clientHeight - padY);

    const layout = chartLayout(available, viewRef.current.showDigital);
    layoutRef.current = layout;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const plotWidth = Math.max(200, Math.floor(cssWidth - GUTTER - PAD_RIGHT));
    plotWidthRef.current = plotWidth;

    for (const c of [canvas, overlay]) {
      c.style.width = `${cssWidth}px`;
      c.style.height = `${layout.total}px`;
      c.width = Math.floor(cssWidth * dpr);
      c.height = Math.floor(layout.total * dpr);
      c.getContext("2d")?.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    if (!columnsRef.current || columnsRef.current.width !== plotWidth) {
      columnsRef.current = new ColumnAggregate(plotWidth);
    }
  }, []);

  // Palette is a theme fact, not a frame fact: read it when the theme moves.
  useEffect(() => {
    const refresh = () => {
      paletteRef.current = readPalette(document.body);
    };
    refresh();
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", refresh);
    const mo = new MutationObserver(refresh);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      mq.removeEventListener("change", refresh);
      mo.disconnect();
    };
  }, []);

  useEffect(() => {
    resize();
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [resize]);

  useEffect(() => {
    resize();
  }, [resize, view.showDigital, view.scale, view.spanSeconds, view.tier, view.yMode]);

  useEffect(() => {
    let raf = 0;
    let lastDraw = 0;

    const frame = (ts: number) => {
      raf = requestAnimationFrame(frame);
      source.tick(ts);
      if (ts - lastDraw < DRAW_INTERVAL_MS) return;
      lastDraw = ts;

      const canvas = canvasRef.current;
      const overlay = overlayRef.current;
      const cols = columnsRef.current;
      const palette = paletteRef.current;
      const layout = layoutRef.current;
      if (!canvas || !overlay || !cols || !palette || !layout) return;
      const ctx = canvas.getContext("2d");
      const octx = overlay.getContext("2d");
      if (!ctx || !octx) return;

      const v = viewRef.current;
      const end = effectiveEnd();
      const span = v.spanSeconds;
      const plotWidth = plotWidthRef.current;
      const tier = chooseTier(span, plotWidth, v.tier);

      cols.collect(source.decimator, tier, end, span);

      // The fit is computed after collecting, from what is actually on screen —
      // fitting to data the window does not contain would be a different claim.
      const range =
        v.yMode === "manual"
          ? v.yRange
          : v.yMode === "fit"
            ? fitRange(fitMin(cols), fitMax(cols))
            : FULL_RANGE;
      rangeRef.current = range;

      drawChart({
        ctx,
        cols,
        layout,
        scale: v.scale,
        range,
        plotWidth,
        now: source.now(),
        labels: labelsRef.current,
        channelRoles: rolesRef.current,
        palette,
      });
      drawCrosshair(octx, cols, hoverRef.current, v.scale, layout, palette, plotWidth, range);

      setTierUsed((prev) => (prev === tier ? prev : tier));
      const buckets = Math.round((span * 1000) / [1, 10, 100, 1000][tier]!);
      setBucketCount((prev) => (prev === buckets ? prev : buckets));
      setRangeUsed((prev) => (prev.lo === range.lo && prev.hi === range.hi ? prev : range));
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [source, effectiveEnd]);

  // Pointer: hover for the crosshair, drag to pan, wheel to zoom.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    let dragging = false;
    let dragStartX = 0;
    let dragStartEnd = 0;
    let queued: Partial<ChartView> | null = null;
    let flushRaf = 0;

    /** One view update per frame, however many events the pointer produced. */
    const queue = (patch: Partial<ChartView>) => {
      queued = { ...queued, ...patch };
      if (flushRaf) return;
      flushRaf = requestAnimationFrame(() => {
        flushRaf = 0;
        const next = queued;
        queued = null;
        if (next) onViewChangeRef.current(next);
      });
    };

    const secondsPerPixel = () => viewRef.current.spanSeconds / Math.max(1, plotWidthRef.current);

    const timeAt = (clientX: number): number => {
      const rect = canvas.getBoundingClientRect();
      const x = clientX - rect.left - GUTTER;
      const end = effectiveEnd();
      return end - (plotWidthRef.current - x) * secondsPerPixel();
    };

    const move = (e: MouseEvent) => {
      if (dragging) {
        const dx = e.clientX - dragStartX;
        // Dragging right walks back in time, the way a strip chart moves.
        queue({ endSeconds: dragStartEnd - dx * secondsPerPixel() });
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const x = Math.floor(e.clientX - rect.left - GUTTER);
      const next = x >= 0 && x < plotWidthRef.current ? x : null;
      if (next === hoverRef.current) return;
      hoverRef.current = next;
      setHoverColumn(next);
    };

    const down = (e: MouseEvent) => {
      if (e.button !== 0) return;
      dragging = true;
      dragStartX = e.clientX;
      dragStartEnd = effectiveEnd();
      setPanning(true);
      hoverRef.current = null;
      setHoverColumn(null);
      e.preventDefault();
    };

    const up = () => {
      if (!dragging) return;
      dragging = false;
      setPanning(false);
    };

    const leave = () => {
      if (hoverRef.current === null) return;
      hoverRef.current = null;
      setHoverColumn(null);
    };

    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      const v = viewRef.current;
      const factor = e.deltaY > 0 ? 1.25 : 0.8;

      if (e.ctrlKey || e.metaKey) {
        // Zoom the current axis about the cursor. Reaching for this is the
        // moment the axis stops being the device's full span, so the mode
        // changes with it and the toolbar says so.
        const layout = layoutRef.current;
        if (!layout || v.scale !== "log") return;
        const rect = canvas.getBoundingClientRect();
        const y = e.clientY - rect.top;
        const pivot = currentAtY(y, layout.plot, rangeRef.current);
        queue({ yMode: "manual", yRange: zoomRange(rangeRef.current, factor, pivot) });
        return;
      }

      const maxSpan = Math.max(MIN_SPAN_S, source.now() - source.historyStart());
      const { span, end } = zoomSpan(v.spanSeconds, effectiveEnd(), factor, timeAt(e.clientX), maxSpan);
      // Zooming out at the live edge should stay live rather than detaching.
      const staysLive = v.endSeconds === null && end >= source.now() - 1e-6;
      queue({ spanSeconds: span, endSeconds: staysLive ? null : end });
    };

    /** Double-click is the way back: live edge, full range. */
    const dblclick = () => {
      queue({ endSeconds: null, yMode: "full", yRange: FULL_RANGE });
    };

    canvas.addEventListener("mousemove", move);
    canvas.addEventListener("mousedown", down);
    canvas.addEventListener("mouseleave", leave);
    canvas.addEventListener("wheel", wheel, { passive: false });
    canvas.addEventListener("dblclick", dblclick);
    window.addEventListener("mouseup", up);
    window.addEventListener("mousemove", move);
    return () => {
      canvas.removeEventListener("mousemove", move);
      canvas.removeEventListener("mousedown", down);
      canvas.removeEventListener("mouseleave", leave);
      canvas.removeEventListener("wheel", wheel);
      canvas.removeEventListener("dblclick", dblclick);
      window.removeEventListener("mouseup", up);
      window.removeEventListener("mousemove", move);
      if (flushRaf) cancelAnimationFrame(flushRaf);
    };
  }, [source, effectiveEnd]);

  return {
    wrapRef,
    canvasRef,
    overlayRef,
    hoverColumn,
    columnsRef,
    layoutRef,
    tierUsed,
    bucketCount,
    rangeUsed,
    panning,
    readStats,
  };
}

function fitMin(cols: ColumnAggregate): number {
  let min = Infinity;
  for (let x = 0; x < cols.width; x++) if (cols.n[x]! > 0 && cols.min[x]! < min) min = cols.min[x]!;
  return min;
}
function fitMax(cols: ColumnAggregate): number {
  let max = -Infinity;
  for (let x = 0; x < cols.width; x++) if (cols.n[x]! > 0 && cols.max[x]! > max) max = cols.max[x]!;
  return max;
}
