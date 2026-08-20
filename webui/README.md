# ppk2lab web console

A browser console for a PPK2: the live current trace, D0-D7 on the same
timeline, the device's state, and the controls that change it.

It is a **viewer first**. Opening it changes nothing about the instrument, and
every state change goes through a dry-run preview before a byte reaches the
wire — the same shape as `configure`, which is a dry run until `--apply`.

> Nothing here has been run against hardware yet. The frontend is complete and
> runs against the shipped `--simulate` profile; the Python supervisor that owns
> a real session is the next piece.

## Where things live

```
webui/                       this directory — the React + TypeScript sources
src/ppk2lab_web/             the Python package that will serve them
src/ppk2lab_web/static/        └─ the build output, committed
```

The split follows what each half needs. The frontend needs a Node toolchain and
never ships; the Python package ships to PyPI and must not need Node. The built
bundle is what crosses between them, which is why it is committed rather than
built at install time. Only someone changing the frontend runs `npm`.

`src/ppk2lab_web` sits beside `src/ppk2lab` rather than inside it so the core
driver keeps `pyserial` as its only dependency. When the server lands its
dependencies will arrive through a `web` extra; there is no such extra yet,
because there is nothing yet to install. Nothing in the core imports from
`ppk2lab_web` either way.

## Working on the frontend

```bash
cd webui
npm install
npm run dev        # http://localhost:5273, live reload
npm test           # decimator, histogram and profile regressions
npm run typecheck
npm run build      # type-checks, then writes ../src/ppk2lab_web/static/
```

`npm run build` is what updates the committed output. Run it before committing a
frontend change, or the served console and the source disagree.

Adding a string means adding it to all four catalogues — TypeScript refuses the
build otherwise, since each translation is typed as `Record<MessageKey, string>`.
`node scripts/add-message.mjs KEY EN ZH-HANT ZH-HANS JA` does all four at once.

## How it is put together

### Sample data does not go through React

The device produces 100,000 samples a second. A `setState` per block would
re-render the tree thousands of times a second to move a picture that can only
change thirty. So the split is:

- `src/chart/useChart.ts` owns a `requestAnimationFrame` loop that advances the
  source, aggregates buckets into a reused buffer, and paints. It redraws at
  30fps, because a full repaint is roughly 13,000 canvas calls.
- React hears about the things that change at human speed — device state, the
  event log, the outcome of a control operation — through
  `useSyncExternalStore`.
- Panels showing continuously moving counters poll at 4 Hz (`useTicker`), which
  is as often as a person can read them.

### Decimation is hierarchical and loss-aware

`src/core/decimator.ts` keeps four tiers — 1 ms, 10 ms, 100 ms, 1 s — where each
is ten of the one below, so a sample is aggregated once and the coarse tiers
cost nothing extra. Every bucket keeps **min, max, sum and count**, never a bare
mean: a mean alone hides exactly the current spikes this instrument was bought
to see (`docs/decimation.md`).

Lost samples are counted per bucket and drawn as hatching. The mean line breaks
at a gap rather than joining across it, because joining would draw a
measurement that was never taken.

### The data source is an interface

`src/data/source.ts` defines what the console reads from and writes to.
`SimulatedSource` runs the shipped `--simulate` profile in the browser;
`WebSocketSource` will talk to the Python supervisor that owns the one open
`PPK2` session. No component knows which it has.

## What the interface must not soften

These are properties of the instrument, not styling choices:

- **The PPK2 never measures voltage.** Every voltage shown is a setpoint or an
  explicit assumption, tagged with its `voltage_basis`. In Ampere mode energy is
  `null` — the correct answer, not a failure — until someone supplies
  `assume_voltage_mv`.
- **DUT power cannot be read back**, and it does not survive the port closing.
  So it is reported as requested, never as confirmed, and `observed_after` says
  which.
- **A quantile served from the 200 nA distribution floor is an upper bound.** It
  is printed with `≤` and marked, because the true value is at or below it.
- **Charge is a floor whenever the window touched loss.** It is labelled as one.
- **Mode and voltage changes require stopping the measurement**, so the console
  says the stream will break before it does. DUT power does not, which is what
  makes an inrush observable live.

## Accessibility and layout

Two themes, driven by tokens: the light palette is defined on bare `:root` and
only the values are redefined for dark, so no colour is declared solely inside a
media query. Status is never colour alone — every badge carries an icon (a CSS
mask tinted by `currentColor`) and a label.

## One screen, no prose

The console is a fixed viewport: identity, state, trace, inspector. Only the
trace stretches, so it takes whatever the other three leave — on a 1007px
viewport that is an 823px canvas, twice what a scrolling layout gave it.
Opening an inspector panel takes height from the chart rather than pushing the
page into a scroll, so the trace never moves out from under the reader.

Nothing on that screen prints a paragraph. Where a number needs a caveat it
carries a coded badge — `W_BELOW_MEASUREMENT_FLOOR`, `charge_is_lower_bound`,
the `voltage_basis` — and the badge's tooltip holds the sentence. The badge is
the part that has to be seen; the sentence is the part that has to be
available. Full explanations live in the drawers, which is where someone who
wants them will be looking.

Layout was audited across widths in four languages: no horizontal page scroll,
no truncated identity labels, and no region that grows the page instead of
scrolling itself.
