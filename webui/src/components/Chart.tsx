import { useCallback, useMemo, useState } from "react";

import type { WindowStats } from "../chart/columns";
import { FULL_RANGE, GUTTER, MIN_SPAN_S } from "../chart/geometry";
import { DEFAULT_VIEW, useChart, type ChartView } from "../chart/useChart";
import { SIM_CHANNEL_ROLE, TIERS } from "../core/constants";
import { fmtCurrent, fmtInt, fmtRelTime } from "../core/format";
import type { DataSource } from "../data/source";
import { useI18n } from "../i18n";
import { Icon } from "./Icons";

interface ChartProps {
  source: DataSource;
  /** Handed back up so the statistics tab reads the same window the chart drew. */
  onHandles: (readStats: () => WindowStats | null) => void;
}

const WINDOWS = [
  { seconds: 1, text: "1s" },
  { seconds: 5, text: "5s" },
  { seconds: 30, text: "30s" },
  { seconds: 120, text: "2m" },
  { seconds: 600, text: "10m" },
  { seconds: 0, text: null },
] as const;

/**
 * The instrument.
 *
 * Everything around the canvas is a control or a unit — no sentences. What the
 * hatching means, why the mean line breaks, and what the range strip is showing
 * are on the legend swatches as tooltips and in the drawers, so the trace keeps
 * the height.
 */
export function Chart({ source, onHandles }: ChartProps) {
  const { t } = useI18n();

  // The view lives here rather than in App: a pan produces a state update per
  // frame, and there is no reason for the inspector to re-render because the
  // pointer moved.
  const [view, setView] = useState<ChartView>(DEFAULT_VIEW);
  const onViewChange = useCallback((patch: Partial<ChartView>) => {
    setView((v) => ({ ...v, ...patch }));
  }, []);

  const labels = useMemo(
    () => ({ range: t("ch_cv_range"), floor: t("ch_cv_floor"), noHistory: t("ch_cv_nohist") }),
    [t],
  );

  const chart = useChart(source, view, onViewChange, labels, SIM_CHANNEL_ROLE);
  onHandles(chart.readStats);

  const live = view.endSeconds === null;
  const maxSpan = Math.max(MIN_SPAN_S, source.now() - source.historyStart());
  const zoom = (factor: number) =>
    onViewChange({ spanSeconds: Math.min(maxSpan, Math.max(MIN_SPAN_S, view.spanSeconds * factor)) });

  const cols = chart.columnsRef.current;
  const hover = chart.hoverColumn;
  const profile = source.demo?.profile();

  return (
    <section className="chartpane">
      <div className="chartbar">
        <div className="grp">
          <span className="lbl">{t("ch_window")}</span>
          <div className="seg">
            {WINDOWS.map((w) => (
              <button
                key={w.seconds}
                type="button"
                aria-pressed={view.spanSeconds === (w.seconds || maxSpan)}
                onClick={() => onViewChange({ spanSeconds: w.seconds || maxSpan })}
              >
                {w.text ?? t("ch_all")}
              </button>
            ))}
          </div>
          <div className="seg">
            <button type="button" onClick={() => zoom(0.5)} title={t("ch_zoomin")} aria-label={t("ch_zoomin")}>
              +
            </button>
            <button type="button" onClick={() => zoom(2)} title={t("ch_zoomout")} aria-label={t("ch_zoomout")}>
              −
            </button>
          </div>
          <button
            type="button"
            className={`btn sm livejump${live ? " hidden" : ""}`}
            onClick={() => onViewChange({ endSeconds: null })}
            title={t("ch_live")}
          >
            <Icon name="activity" />
            <span>{t("ch_live")}</span>
          </button>
        </div>

        <div className="grp">
          <span className="lbl">{t("ch_down")}</span>
          <div className="seg">
            <button type="button" aria-pressed={view.tier === -1} onClick={() => onViewChange({ tier: -1 })}>
              auto
            </button>
            {TIERS.map((tier, i) => (
              <button
                key={tier.label}
                type="button"
                aria-pressed={view.tier === i}
                onClick={() => onViewChange({ tier: i })}
              >
                {tier.label.replace(" ", "")}
              </button>
            ))}
          </div>
        </div>

        <div className="seg">
          <button type="button" aria-pressed={view.scale === "log"} onClick={() => onViewChange({ scale: "log" })}>
            {t("ch_log")}
          </button>
          <button
            type="button"
            aria-pressed={view.scale === "lin"}
            onClick={() => onViewChange({ scale: "lin" })}
            title={t("ch_lin_note")}
          >
            {t("ch_lin")}
          </button>
        </div>

        <div className="grp">
          <span className="lbl">{t("ch_yrange")}</span>
          <div className="seg">
            <button
              type="button"
              aria-pressed={view.yMode === "full"}
              onClick={() => onViewChange({ yMode: "full", yRange: FULL_RANGE })}
            >
              {t("ch_y_full")}
            </button>
            <button
              type="button"
              aria-pressed={view.yMode === "fit"}
              onClick={() => onViewChange({ yMode: "fit" })}
            >
              {t("ch_y_fit")}
            </button>
            {view.yMode === "manual" && (
              <button type="button" aria-pressed>
                {fmtCurrent(chart.rangeUsed.lo, 0)}–{fmtCurrent(chart.rangeUsed.hi, 0)}
              </button>
            )}
          </div>
        </div>

        <button
          type="button"
          className="btn sm"
          aria-pressed={view.showDigital}
          onClick={() => onViewChange({ showDigital: !view.showDigital })}
        >
          D0–D7
        </button>

        {source.demo && profile && (
          <div className="seg" title={t(profile === "demo" ? "sc_demo_desc" : "sc_idle_desc").replace(/<[^>]+>/g, "")}>
            <button type="button" aria-pressed={profile === "demo"} onClick={() => source.demo?.setProfile("demo")}>
              {t("sc_demo")}
            </button>
            <button type="button" aria-pressed={profile === "idle"} onClick={() => source.demo?.setProfile("idle")}>
              {t("sc_idle")}
            </button>
          </div>
        )}

        <div className="legend" aria-hidden="true">
          <i className="swatchband" title={t("ch_lg_band")} />
          <i className="swatchline" title={t("ch_lg_mean")} />
          <i className="swatchgap" title={t("ch_lg_gap")} />
        </div>

        <span className="lbl tierline">
          {TIERS[chart.tierUsed]!.label} · {fmtInt(chart.bucketCount)} bkt ·{" "}
          {view.tier >= 0 ? t("ch_manual") : "auto"}
        </span>
      </div>

      <div
        className={`canvaswrap${chart.panning ? " panning" : ""}${live ? "" : " detached"}`}
        ref={chart.wrapRef}
        title={t("ch_gesture")}
      >
        <canvas ref={chart.canvasRef} />
        <canvas id="overlay" ref={chart.overlayRef} />
        {cols && hover !== null && (
          <Tooltip cols={cols} column={hover} now={source.now()} tier={chart.tierUsed} />
        )}
        {!live && <span className="pausedflag pill warn">{t("ch_paused")}</span>}
      </div>
    </section>
  );
}

/**
 * What one column actually holds.
 *
 * The digital readout is a strip rather than a bit string because after
 * decimation "steady high" and "toggled within this bucket" are different
 * facts, and `1` cannot express the second one.
 */
function Tooltip({
  cols,
  column,
  now,
  tier,
}: {
  cols: NonNullable<ReturnType<typeof useChart>["columnsRef"]["current"]>;
  column: number;
  now: number;
  tier: number;
}) {
  const { t } = useI18n();
  const has = cols.n[column]! > 0;
  const span = cols.t1 - cols.t0;
  const time = cols.t0 + ((column + 0.5) / cols.width) * span;
  const ranges = cols.ranges(column);
  const gap = cols.gap[column]!;

  return (
    <div
      className="tip on"
      style={{
        left: Math.min(12 + GUTTER + column + 18, 12 + GUTTER + cols.width - 200),
        top: 12,
      }}
    >
      <div className="t">
        {fmtRelTime(time - now)} · {TIERS[tier]!.label}
      </div>
      <dl>
        <dt>max</dt>
        <dd>{has ? fmtCurrent(cols.max[column]!) : "—"}</dd>
        <dt>mean</dt>
        <dd>{has ? fmtCurrent(cols.mean(column)) : "—"}</dd>
        <dt>min</dt>
        <dd>{has ? fmtCurrent(cols.min[column]!) : "—"}</dd>
        <dt>n</dt>
        <dd>{fmtInt(cols.n[column]!)}</dd>
        {gap > 0 && (
          <>
            <dt style={{ color: "var(--loss)" }}>gap</dt>
            <dd style={{ color: "var(--loss)" }}>{fmtInt(gap)}</dd>
          </>
        )}
        <dt>{t("ch_cv_range")}</dt>
        <dd>{ranges.length ? ranges.join(",") : "—"}</dd>
        <dt>D7…D0</dt>
        <dd>
          <BitStrip any={cols.logicAny[column]!} all={cols.logicAll[column]!} has={has} />
        </dd>
      </dl>
    </div>
  );
}

function BitStrip({ any, all, has }: { any: number; all: number; has: boolean }) {
  const W = 10;
  const H = 13;
  const cells: React.ReactElement[] = [];
  for (let ch = 7; ch >= 0; ch--) {
    const x = (7 - ch) * W;
    if (!has) {
      cells.push(
        <rect key={ch} x={x + 1} y={(H - 1) / 2} width={W - 2} height={1} fill="currentColor" opacity={0.25} />,
      );
      continue;
    }
    const a = (any >> ch) & 1;
    const b = (all >> ch) & 1;
    if (a !== b) {
      cells.push(<rect key={ch} x={x + 1} y={2} width={W - 2} height={H - 4} fill="var(--trace)" opacity={0.42} />);
    } else if (a) {
      cells.push(<rect key={ch} x={x + 1} y={2} width={W - 2} height={2.2} fill="var(--trace)" />);
    } else {
      cells.push(<rect key={ch} x={x + 1} y={H - 4.2} width={W - 2} height={2.2} fill="var(--trace)" />);
    }
  }
  return (
    <svg width={W * 8} height={H} viewBox={`0 0 ${W * 8} ${H}`} aria-hidden="true">
      {cells}
    </svg>
  );
}
