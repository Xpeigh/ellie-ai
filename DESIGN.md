# Design

The visual system for Ellie's chat window (`static/index.html`, `style.css`, `app.js`). It is a menu screen from Elite Dangerous, not a "sci-fi dashboard": every value below was measured from real screenshots of the commander's own game, or is labeled as derived.

## Principles

- Flat. Fills and 2px outlines only. No glow, blur, drop shadow, gradient, scanline, corner cut, or glass.
- One weight. The game's UI is regular-weight uppercase; so is this. Emphasis is color or size, never faux bold or italic.
- Numerals larger than their labels (the game sets "CLASS **2**" and "**10.62**MW" this way).
- Selection is gold on dark gold. Orange is reserved for the credit balance and the orange theme.
- Nothing decorative. Every colored element is a state, a selection, or an action.

## Typography

**Electrolize** Regular, self-hosted (`static/fonts/`, SIL OFL 1.1). Chosen by rendering seven open-license candidates beside crops of the game's lettering; closest match for stroke weight and squarish rounded bowls (O, D, Q, numeral 2). The game's actual face is Eurostile-family and licensed, so it is not used.

Fixed rem scale, ratio ~1.2: 0.75 / 0.8125 / 1 / 1.1875 / 1.4375 / 1.75. Uppercase labels 0.75-0.8125rem at 0.06em tracking; body 1rem.

## Themes

Tokens are CSS custom properties on `:root`; `:root[data-theme="orange"]` overrides them. Stored in `localStorage` (`ellie-theme`).

| Token | Violet (sampled) | Orange (derived) |
|---|---|---|
| panel / panel-2 / panel-3 | #14012c / #100025 / #0e0021 | #2c1401 / #251000 / #210e00 |
| line | #2c0065 | #652c00 |
| accent | #6f00ff | #ff6f00 |
| accent-dim | #5a00cf | #cf5a00 |
| on-accent | #ffffff | #000000 |
| select-bg / select-ink | #221c02 / #fdd700 | #ff6f00 / #000000 |
| text / strong | #f3f1f3 / #ffffff | same |
| dim | #b9aed0 (game labels #b7b2bc, tinted) | #c2b3a8 |
| info / credit / danger | #00a4cd / #fe6f00 / #ff5a4d | same |

**Provenance.** Violet comes from pixel sampling of the commander's Jan 2026 menu screenshots (EDHM UI theme "Janus Luxe - Midnight"). Orange is *derived*, not sampled from a vanilla install: the violet values run back through EDHM's channel-rotation color matrix (`XML-Profile.ini`, rows 0,0,1 / 1,0,0 / 0,1,0), which maps the game's orange balance icon (254,111,0) to (111,0,255). Orange selection (orange fill, black ink) and `danger` were chosen, not measured. `--faint` and the tinted `--dim` are design choices for contrast.

## Components

- **Top bar** (panel-3): wordmark, connection state (square: cyan online, gold setup, red offline), balance (orange triangle glyph, value, CR), theme and status buttons.
- **Console rows**: 96px label column + text. Commander rows panel-3, Ellie rows panel-2 with an 8px accent square. Emphasis from the model's `**bold**` renders as gold (`--emph`), not bold.
- **Input bar**: accent square with chevron, field, SEND. Focus is a 2px gold outline on the whole bar.
- **Status panel** (panel, 340px): section titles, big value + dim sub-line, label/value rows. Docked state uses the selection style. Materials stack label over value so long numbers never wrap.
- **Buttons**: panel fill, 1px line; hover accent fill; active accent-dim; disabled faint.
- **Value change**: a 1.4s gold outline that fades. No color-glow.
- **Narrow (<=860px)**: status panel becomes a right-hand drawer (STATUS button, backdrop, Esc closes).

## Accessibility

All text pairs measured >= 5.0:1 in both themes (lowest: `--faint` on panel, orange, 5.02:1). Keyboard: Enter sends, Esc closes the drawer, `:focus-visible` outlines everywhere. `prefers-reduced-motion` disables animation.

## Verification (2026-09-21)

- `impeccable detect` on the shipped page: 0 findings (was 8: glow, hairline+wide shadow, scanline stripes, 5x low contrast).
- Rendered at 1440x900 and 390x844 in both themes; a compound question answered end to end through two real tool calls.
- The page's only network host is `localhost:8420`; no external requests.
