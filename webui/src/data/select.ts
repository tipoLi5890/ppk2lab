import { SimulatedSource } from "./simulated";
import type { DataSource } from "./source";
import { WebSocketSource } from "./websocket";

/**
 * Which device this console talks to, and how it is decided.
 *
 * Two rules matter more than the table below.
 *
 * **Same origin, resolved at runtime.** The built bundle is committed into the
 * Python wheel and shipped to PyPI, so a host baked in at build time would be
 * permanently wrong for every user. In production the server serves this page,
 * so `location.host` is already the right answer and the bundle contains no
 * host and no port at all.
 *
 * **Never fall back to the simulator.** If the socket cannot connect the
 * console says so and offers a button. Falling back silently would put a
 * synthetic 12 mA burst on screen and label it live, which is the single worst
 * thing this console could do.
 */
export type SourceChoice =
  | { kind: "simulated" }
  | { kind: "websocket"; url: string };

export function resolveSource(page: URL, dev: boolean): SourceChoice {
  const requested = page.searchParams.get("source");
  if (requested === "sim") return { kind: "simulated" };
  if (requested === "ws") return { kind: "websocket", url: websocketUrl(page) };
  // `npm run dev` with nothing else running still works, which is what makes
  // the frontend workable on its own. `?source=ws` opts into the proxy.
  if (dev) return { kind: "simulated" };
  return { kind: "websocket", url: websocketUrl(page) };
}

export function websocketUrl(page: URL): string {
  const override = page.searchParams.get("ws");
  if (override) return override;
  const scheme = page.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${page.host}/ws`;
}

let instance: DataSource | null = null;

/**
 * The one source for the life of the tab.
 *
 * Memoised at module scope rather than in a `useRef`. React Fast Refresh
 * remounts `App` on every edit to it, which would build a fresh source -- and
 * therefore a fresh socket -- dozens of times in a dev session, against a
 * server whose whole premise is that one process owns the port. StrictMode's
 * double mount is the same problem in a smaller form.
 *
 * Refcounting `subscribe()` would be the elegant alternative and does not work
 * here: `hooks.ts` passes a fresh closure on every render, so React
 * unsubscribes and resubscribes at render rate and the socket would flap.
 */
export function getSource(): DataSource {
  if (!instance) instance = build(resolveSource(new URL(location.href), import.meta.env.DEV));
  return instance;
}

export function build(choice: SourceChoice): DataSource {
  if (choice.kind === "simulated") return new SimulatedSource();
  return new WebSocketSource({ url: choice.url });
}

/** Tests and hot-module replacement only. */
export function disposeSource(): void {
  const current = instance as WebSocketSource | null;
  instance = null;
  current?.close?.();
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => disposeSource());
}
