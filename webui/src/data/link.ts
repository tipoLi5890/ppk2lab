/**
 * The socket, with reconnection and request correlation. No domain knowledge.
 *
 * The socket is built by an injected factory rather than by `new WebSocket()`
 * inline, and that single decision is what makes reconnection, backoff,
 * timeouts and a drop mid-apply testable without a browser or a server.
 */

export interface WebSocketLike {
  binaryType: string;
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number; reason: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export type Phase = "connecting" | "open" | "reconnecting" | "closed";

export interface LinkOptions {
  url: string;
  socketFactory?: (url: string) => WebSocketLike;
  now?: () => number;
  schedule?: (fn: () => void, ms: number) => number;
  cancel?: (handle: number) => void;
  /** Uniform in [0,1). Injected so the jitter is reproducible in a test. */
  random?: () => number;
}

export interface LinkHandlers {
  onJson: (message: Record<string, unknown>) => void;
  onBinary: (frame: ArrayBuffer) => void;
  onPhase: (phase: Phase, info: PhaseInfo) => void;
}

export interface PhaseInfo {
  attempt: number;
  /** `now()` value of the next attempt, or null when not waiting. */
  nextAttemptAtMs: number | null;
  /** A message key, never a raw sentence. */
  reasonKey: string | null;
}

/**
 * Backoff, in milliseconds. Capped rather than unbounded: a bench console left
 * open overnight should come back when the server is restarted, and a ten
 * second ceiling is short enough that it does.
 */
const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000, 10_000];
const JITTER = 0.2;

/** Close codes that mean "do not come back". */
const TERMINAL_CLOSE = new Set([1008]);

export class LinkClosed extends Error {
  constructor(readonly reasonKey: string) {
    super(reasonKey);
  }
}

interface Pending {
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: unknown) => void;
  timer: number;
}

export class Link {
  phase: Phase = "connecting";
  attempt = 0;
  nextAttemptAtMs: number | null = null;
  reasonKey: string | null = null;

  private readonly url: string;
  private readonly make: (url: string) => WebSocketLike;
  private readonly now: () => number;
  private readonly schedule: (fn: () => void, ms: number) => number;
  private readonly cancel: (handle: number) => void;
  private readonly random: () => number;

  private socket: WebSocketLike | null = null;
  private pending = new Map<string, Pending>();
  private nextId = 0;
  private retryTimer: number | null = null;
  private stopped = false;
  /** Set once a hello has been seen, which is what proves the link is usable. */
  private established = false;

  constructor(
    options: LinkOptions,
    private readonly handlers: LinkHandlers,
  ) {
    this.url = options.url;
    this.make = options.socketFactory ?? ((u) => new WebSocket(u) as unknown as WebSocketLike);
    this.now = options.now ?? (() => performance.now());
    this.schedule = options.schedule ?? ((fn, ms) => setTimeout(fn, ms) as unknown as number);
    this.cancel = options.cancel ?? ((h) => clearTimeout(h));
    this.random = options.random ?? Math.random;
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  close(): void {
    this.stopped = true;
    if (this.retryTimer !== null) this.cancel(this.retryTimer);
    this.retryTimer = null;
    this.failAll("er_offline");
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.setPhase("closed", null);
  }

  /** Stop retrying, for a mismatch no retry can fix. */
  giveUp(reasonKey: string): void {
    this.stopped = true;
    this.reasonKey = reasonKey;
    this.socket?.close();
    this.socket = null;
    this.setPhase("closed", reasonKey);
  }

  /** Called by the owner once a hello has been accepted. */
  markEstablished(): void {
    // Reset the backoff on a *usable* link, not merely an accepted socket: a
    // server that accepts and immediately closes -- wrong protocol, port busy
    // -- would otherwise be reconnected to in a hot loop.
    this.established = true;
    this.attempt = 0;
    this.setPhase("open", null);
  }

  get connected(): boolean {
    return this.phase === "open";
  }

  request(
    op: string,
    fields: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      const socket = this.socket;
      if (!socket || this.phase !== "open") {
        // Nothing was sent, so nothing happened. That is a different fact from
        // the two below and the console says so differently.
        reject(new LinkClosed("er_offline"));
        return;
      }
      const id = String(++this.nextId);
      const timer = this.schedule(() => {
        this.pending.delete(id);
        reject(new LinkClosed("er_timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        socket.send(JSON.stringify({ id, op, ...fields }));
      } catch {
        this.settle(id, null, new LinkClosed("er_offline"));
      }
    });
  }

  send(message: Record<string, unknown>): void {
    if (this.socket && this.phase === "open") this.socket.send(JSON.stringify(message));
  }

  // -- internals --------------------------------------------------------
  private open(): void {
    if (this.stopped) return;
    this.setPhase(this.established ? "reconnecting" : "connecting", this.reasonKey);
    let socket: WebSocketLike;
    try {
      socket = this.make(this.url);
    } catch {
      this.scheduleRetry("conn_nosrv");
      return;
    }
    socket.binaryType = "arraybuffer";
    this.socket = socket;

    socket.onopen = () => {
      this.nextAttemptAtMs = null;
    };
    socket.onmessage = (event) => {
      const data = event.data;
      if (typeof data === "string") {
        let parsed: Record<string, unknown>;
        try {
          parsed = JSON.parse(data) as Record<string, unknown>;
        } catch {
          return;
        }
        if (parsed.type === "result" && typeof parsed.id === "string") {
          this.settle(parsed.id, parsed, null);
          return;
        }
        this.handlers.onJson(parsed);
        return;
      }
      if (data instanceof ArrayBuffer) this.handlers.onBinary(data);
    };
    socket.onerror = () => {
      /* onclose always follows; nothing useful is carried here. */
    };
    socket.onclose = (event) => {
      this.socket = null;
      // Every request in flight is answered, and with the truth: it was sent
      // and never acknowledged, so whether it landed is unknown. It is
      // deliberately not retried -- replaying a state change that may already
      // have taken effect is not safe.
      this.failAll("er_disconnected");
      if (TERMINAL_CLOSE.has(event.code)) {
        this.giveUp("conn_refused");
        return;
      }
      this.scheduleRetry(this.established ? null : "conn_nosrv");
    };
  }

  private scheduleRetry(reasonKey: string | null): void {
    if (this.stopped) return;
    const base = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)]!;
    const delay = Math.round(base * (1 + (this.random() * 2 - 1) * JITTER));
    this.attempt += 1;
    this.nextAttemptAtMs = this.now() + delay;
    this.reasonKey = reasonKey;
    this.setPhase(this.established ? "reconnecting" : "connecting", reasonKey);
    this.retryTimer = this.schedule(() => {
      this.retryTimer = null;
      this.open();
    }, delay);
  }

  /** Reconnect immediately, for a signal that waiting is pointless. */
  retryNow(): void {
    if (this.stopped || this.phase === "open") return;
    if (this.retryTimer !== null) this.cancel(this.retryTimer);
    this.retryTimer = null;
    this.nextAttemptAtMs = null;
    this.open();
  }

  private settle(id: string, value: Record<string, unknown> | null, error: unknown): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    this.cancel(pending.timer);
    if (error) pending.reject(error);
    else pending.resolve(value!);
  }

  private failAll(reasonKey: string): void {
    for (const [id] of [...this.pending]) this.settle(id, null, new LinkClosed(reasonKey));
  }

  private setPhase(phase: Phase, reasonKey: string | null): void {
    this.phase = phase;
    this.reasonKey = reasonKey;
    if (phase === "open") this.nextAttemptAtMs = null;
    this.handlers.onPhase(phase, {
      attempt: this.attempt,
      nextAttemptAtMs: this.nextAttemptAtMs,
      reasonKey,
    });
  }
}
