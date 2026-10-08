# Brand

athena is one calm mind running through many hands. The brand is **the red thread**: Athena was the goddess of weaving, and athena is the one thread that runs through every worker and holds the work together.

## Mark

A trefoil knot made from one continuous thread: three loops, and every crossing alternates over and under. It is drawn as three identical strands of cubic Béziers, each rotated 120°, with stroke width 0.6 of the knot scale and round ends.

- `assets/logo-mark.svg` follows the reader's light or dark scheme. `logo-mark.png` (madder) and `logo-mark-dark.png` (lit madder) are 1024 px.
- `assets/logo-lockup.svg` and `logo-lockup-dark.svg`: the mark and the wordmark, also as 1024 px PNGs. In a README, switch between them with `<picture>` and `prefers-color-scheme`.
- `assets/social-card.png`: 1280×640, for the repository's social preview (Settings, General, Social preview).

Usage:

- Keep clear space of half the mark's height on every side.
- Smallest size: 16 px for the mark, 120 px wide for the lockup.
- The mark is always one colour: madder on light, lit madder on dark. Never outline it, add a gradient or shadow, or change the crossings.
- The wordmark is lower case `athena`. Do not set it in another typeface or retype it; use the outlined files.

## Colour

| Name | Hex | Use |
|---|---|---|
| Linen | `#F3EEE4` | Light ground |
| Indigo | `#1E2350` | Ink and the wordmark on light |
| Madder | `#B8321F` | athena's thread on light. Only athena is red. |
| Indigo night | `#14172E` | Dark ground |
| Linen, lit | `#ECE5D6` | Ink and the wordmark on dark |
| Madder, lit | `#E0573D` | athena's thread on dark |
| Muted | `#5B5E78` / `#A3A3B8` | Labels, light / dark |

Contrast (WCAG 2): indigo on linen 12.9:1, madder on linen 5.2:1, linen-lit on indigo night 14.1:1, lit madder on indigo night 4.7:1. On GitHub's own grounds, madder on white is 6.0:1 and lit madder on `#0d1117` is 5.1:1. Muted labels are 5.5:1 on linen and 7.1:1 on indigo night.

## Type

- **Wordmark: Instrument Serif** (Instrument, SIL Open Font License 1.1). A condensed display serif with calm contrast: it reads like a name signed on a letter rather than a tool, and its tall narrow stems echo warp threads. The wordmark is outlined to paths, so nothing depends on installed fonts.
- **Labels and captions: Instrument Sans** (Instrument, SIL Open Font License 1.1), weight 500 to 600, also outlined in the graphics.
- No monospace in the brand. Code stays in code blocks.

## Motion

The three README graphics (`assets/one-thread.svg`, `ticket-path.svg`, `watch.svg`) share one protagonist, the red thread, and these rules:

1. Only athena is red. Workers, lines and labels stay neutral.
2. Things appear by a moving edge (the thread drawing itself, a line running in) in the direction of cause and effect, never by a uniform fade.
3. Easing: arrivals `cubic-bezier(.16,1,.3,1)`; exits `cubic-bezier(.7,0,.84,0)` at about 60% of the entry time; travel `cubic-bezier(.65,0,.35,1)`; one small overshoot `cubic-bezier(.34,1.4,.64,1)` at most per scene. Linear only for the steady stream of routine progress.
4. Stagger 60 to 110 ms, never all at once. Hold at least 600 ms after each arrival.
5. Loops are 10 s and end on their first frame.
6. No glows, gradients, particles or idle pulsing.

Technical constraints, so the files render on GitHub through `<img>`:

- Self-contained SVG with CSS keyframes only: no script, `foreignObject`, external fonts, images or CSS.
- The un-animated state is the most explanatory frame. Motion lives inside `@media (prefers-reduced-motion: no-preference)`, so reduced motion shows that still. (SMIL ignores reduced motion, so avoid it.)
- Light and dark come from `@media (prefers-color-scheme: dark)` variables, with the graphic's own ground.
- 960 px wide, under 60 KB each, labels outlined, tickets fictional (`abc-101`).
