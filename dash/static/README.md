# Fonts

The dashboard embeds these files unmodified, so it loads no font from another host.

| File | Source | Licence |
|---|---|---|
| `InstrumentSerif-Regular.woff2` | https://github.com/Instrument/instrument-serif, `fonts/webfonts/`, commit `65c0ef2` | SIL Open Font License 1.1, `InstrumentSerif-OFL.txt` |
| `InstrumentSans.woff2` | https://github.com/Instrument/instrument-sans, `fonts/webfonts/InstrumentSans[wdth,wght].woff2`, commit `7fa2230` | SIL Open Font License 1.1, `InstrumentSans-OFL.txt` |

The OFL allows bundling the fonts with software as long as each copy keeps its copyright notice and licence, which the two `-OFL.txt` files are. The fonts are not sold on their own.

# Icons

The page's sprite (`../page.html`, `sprite`) carries 24 icons from [Lucide](https://lucide.dev) v0.460.0 (`lucide-static`, `icons/<name>.svg`), unmodified but for being wrapped in `<symbol id="i-<name>">`: the verbs an ask's option may show (`../icons.txt` lists them). Lucide is under the ISC License, `Lucide-LICENSE.txt`, which also carries Feather's MIT notice for the icons it inherits.
