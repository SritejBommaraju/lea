# LEA design: the inspector's codex

Draft 2, 2026-10-24, after moodboard review 1 (§11). The direction for the iOS app, drawn from the moodboard and written so the Figma prototypes
and the next `Theme.swift` change can be built from it without guessing. Reference numbers (No. 01-30) point at the
[moodboard](https://claude.ai/artifact/NeND4dq9nJpS9FyS1QkpzL) (private until shared). Tokens in code:
[Theme.swift](Theme.swift); capture wiring: [INTEGRATION.md](INTEGRATION.md).

Status: direction agreed → Figma framework built ([file](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u)): tokens,
components and screens 01-08 in both modes → **team review** → `Theme.swift` PR → screens. No code changes until the
review settles the design. In that file Inter stands in for SF Pro,
JetBrains Mono for SF Mono and drawn icons for SF Symbols, because the system fonts do not render there.

## 1. The idea

LEA reads like Leonardo's notebook used as a working instrument. Ink on paper by day, walnut and brass by night.
The camera never turns into paper; only the ink lines come with you. The report reads like a museum label: printed
facts, with judgement written in the margin.

The look comes from line, type and annotation. It never comes from texture.

## 2. Principles

1. **Ink, not texture.** Paper is a flat warm tone. No parchment images, paper grain, wood grain, torn edges or wax
   seals. The notebook feeling comes from hairlines, construction circles, leader lines and type (No. 01, 03).
2. **Red means danger.** Oxblood (light) and vermilion (dark) mark safety problems and nothing else. Brand actions
   are ink, so a red button never competes with a safety card (No. 11).
3. **Printed facts, judgement in the margin.** What the app measured or saw is set upright. Confidence and
   likelihood words, which are judgement, are set in italic in the margin (No. 18, 19). Confidence stays a word (I6).
4. **Unknown is a construction line.** Anything not inspected or not judged is drawn dashed, the way an unfinished
   sketch is drawn. Anything judged is inked solid. This is I3 and I4 made visible: `not_inspected` can never look
   like `ok`. Dashes mean unknown and nothing else, so decorative construction lines are drawn solid.
5. **Evidence keeps its colour.** Photos, crops and the 3D scan are never tinted, sepia-toned or turned into line
   art: colour is evidence (scorch marks, corrosion, "colour doesn't match"). Only our marks around them are inked.
6. **The camera gets line art only.** Chalk lines with a sepia keyline, smoked fills, no paper, no blur, no
   shadows, no animated gradients, ≤ 10 changes a second (the rules in INTEGRATION.md stand).
7. **One action per screen, in ink.** The big button is the darkest thing on paper and the lightest thing on
   walnut or camera.
8. **Colour is never the only cue.** Every tone comes with an icon and a word; over the camera, a shape.

## 3. Colour

Sampled from No. 03-07 and tuned so every text colour passes 4.5:1 on canvas, card and its own tint, and every status pair
stays apart for deuteranopia and protanopia (CIELAB ΔE on Machado-simulated colours).

### Paper (light) and Walnut (dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `canvas` | `#F4EEE3` paper | `#16110D` walnut | Screen background |
| `surface` | `#FBF8F1` card | `#221A14` walnut card | Cards and grouped rows |
| `well` | `#EDE4D6` | `#2E241C` | Inputs, thumbnails while loading |
| `hairline` | ink at 14 % | brass at 28 % | 0.5 pt card edges and ruled rows |
| `ink` | `#2B2118` | `#EDE3CF` pearl | Primary text; the brand action colour |
| `sepia` | `#6B5D4F` | `#A99880` | Secondary text, neutral tone (5.5 / 6.1 : 1) |
| `faint` | `#8A7A68` | `#7D6E5B` | Placeholders and disabled only (< 4.5 : 1, never for text people must read) |
| `brass` | (not used) | `#C9A86A` | Dark-mode hairlines, rule ornaments, wordmark |

Buttons: paper label on ink (13.6 : 1) in light, ink label on pearl (12.4 : 1) in dark.

### Status tones

| Tone | Light | Dark | Means | Contrast light / dark | On its own tint |
|---|---|---|---|---|---|
| `good` sage | `#5E6834` | `#B8D08F` | Verdict "No visible problems", dome done | 5.2 / 10.2 | 4.6 / 7.1 |
| `caution` ochre | `#805C00` | `#E6AA3E` | Severity `functional`, fixable cues | 5.3 / 8.3 | 4.6 / 6.1 |
| `danger` oxblood / vermilion | `#8A1E12` | `#EC7457` | Severity `safety`, blocking cues, failures | 8.0 / 5.9 | 6.7 / 4.6 |
| `cosmetic` woad | `#3B5B7D` | `#86A9C9` | Severity `cosmetic` | 6.1 / 7.0 | 5.3 / 5.3 |
| `neutral` sepia | `#6B5D4F` | `#A99880` | `not_inspected`, `insufficient_evidence` (with the dashed form) | 5.5 / 6.1 | 4.8 / 4.7 |

Contrast is the worst of canvas and card. A tint is the tone at 10 % (light) or 16 % (dark) over the card. Draft 1's
values failed there (light sage 4.1, ochre 4.0; dark vermilion 3.8, sepia 4.2 : 1), and tags put tone text on a
tint, so the tones moved. Tags are now outlined (§5), which leaves the safety card as the one tinted surface.

Separation, worst pair across normal, deuteranopia and protanopia vision (CIELAB ΔE76):

| | Today's theme | This palette |
|---|---|---|
| Light | ΔE 15.8, but good 3.1, caution 3.4, danger 4.3 : 1 as text | ΔE 13.3 (caution vs danger, deuteranopia), all ≥ 4.5 : 1 |
| Dark | ΔE 12.6 (good vs danger, deuteranopia) | ΔE 19.3 (good vs danger, deuteranopia) |

Light lost a little separation to buy contrast; the icon and word on every tone carry the rest.

### Camera (same in both modes)

| Token | Value | Use |
|---|---|---|
| `chalk` | `#F6EFE2` | Every line and label over the camera |
| `keyline` | `#2A1E14` at 80 % | A stroke 2 pt wider under each chalk line (1 pt each side), so it reads on bright and dark scenes without a shadow (8.5 : 1 on white) |
| `smoke` | `#1B140F` at 72 % | Solid fill behind the cue, pills, dial disc and round buttons (replaces black at 45-58 %) |
| `azurite` | `#9EC3E6` | The next dome cell and the locked brackets. Only exists over the camera |

Measured over a white scene, the worst case: chalk text on smoke 6.4 : 1 (3.9 at 58 %), empty dial cells at 55 %
chalk 3.2 : 1 (1.7 at 35 %). The moodboard's stress test shows both.

Cue colours over the camera reuse the walnut status set. The glyph on a cue disc is `#1B140F` (8.8 : 1 on ochre,
6.2 on vermilion, 10.8 on sage). Azurite is at least ΔE 31 from every cue colour under all three visions; brass was
tried and sat too close to sage and ochre. Azurite and chalk are the same lightness (1.2-1.4 : 1), so azurite is
never the only thing marking a state: the next cell also gets a rim tick, the locked brackets a heavier stroke.

### Rules

- Brass never appears in light mode and never on a status element.
- Azurite never appears off the camera. On app screens, "next" and "selected" are ink.
- No literal colours outside `Theme.swift`. Today `CueBanner.Tone.fill` and `LockBoxSpec.uiColor` hold hex values;
  the Theme PR moves them onto camera tokens.

## 4. Type

Four voices, modelled on a museum label (No. 18, 21).

| Voice | Face | Licence | Where |
|---|---|---|---|
| Display | EB Garamond, 500-600 | SIL OFL 1.1, bundled | Screen titles, verdict, section titles, part names, wordmark |
| Reading | SF Pro (system) | Apple system font, not bundled | Body, instructions, reasons, buttons, cues |
| Label | SF Mono (system, `.monospaced`) | Apple system font, not bundled | Numbers and labels: "No. 03", "PHOTO 7 OF 12", dates, counters, tags |
| Margin | EB Garamond Italic, 500 | SIL OFL 1.1, bundled (same family) | Confidence and likelihood words, margin notes. Three words at most |

Token mapping (every custom face scales with `Font.custom(_:size:relativeTo:)`):

| Token | Today | New |
|---|---|---|
| `heroTitle` | SF Rounded bold, largeTitle | EB Garamond Medium 36, relative to `.largeTitle` |
| `screenTitle` | SF Rounded bold, title | EB Garamond Medium 30, relative to `.title` |
| `sectionTitle` | SF Rounded semibold, title3 | EB Garamond Medium 22, relative to `.title3` |
| `cardTitle` | SF Pro headline | EB Garamond SemiBold 19, relative to `.headline` |
| `buttonLabel` | SF Rounded semibold | SF Pro semibold, `.headline` |
| `pillLabel` | SF Rounded semibold footnote | SF Mono semibold `.caption`, uppercase, tracking 0.06 em |
| `cue` | SF Rounded semibold callout | SF Pro semibold `.callout` (stays sans for legibility over the camera) |
| `metric` | SF Rounded semibold title2 | SF Mono medium `.title2` |
| `wordmark` | SF Rounded bold 30, fixed | EB Garamond SemiBold 30, fixed, tracking 0.04 em |
| `label` (new) | | SF Mono semibold `.caption`, uppercase, tracking 0.1 em |
| `annotation` (new) | | EB Garamond Italic Medium 19, relative to `.callout` |

- Body text stays SF Pro at the system text styles, so Dynamic Type, Bold Text and accessibility sizes behave as
  they do now.
- Serif sizes are 2 pt over their text style because EB Garamond's x-height is small.
- No handwriting font. Draft 1 used Caveat; review 1 dropped it: a modern marker hand reads as a sticky note rather
  than a codex, it dresses a model's judgement as a person's writing, and script is the hardest voice to read for
  dyslexic and low-vision readers. Italic type was cut from the Renaissance hand, so it keeps the idea.
- **Licence check needed.** PLAN.md L2 lists Apache-2.0, MIT, BSD, CC-BY and the Apple Sample Code License. SIL OFL
  1.1 is a permissive font licence but is not on that list. The Theme PR adds "SIL OFL 1.1 for fonts" to L2 (owner
  sign-off) and EB Garamond to `ios/THIRD_PARTY_NOTICES.md`.

## 5. Line, shape and space

| | Today | New |
|---|---|---|
| Card radius | 22 | 12: labels pinned on paper, not bubbles |
| Medium / small / thumb radius | 14 / 8 / 12 | 10 / 6 / 6 |
| Primary button | 56 pt capsule | 56 pt capsule (unchanged shape, ink fill) |
| Card edge | 0.5 pt hairline | 0.5 pt hairline; dark mode uses brass |
| Inside a card | grey wells | 0.5 pt ruled rows between fields (No. 20) |
| Tags | tone text on a tinted capsule | 0.5 pt tone outline, tone icon or dot, tone word, no tint (No. 24) |
| Spacing | `Spacing` | Unchanged |

Line weights: 0.5 pt rules; 1 pt construction lines and empty dome cells; 1.5 pt searching brackets and drawings;
2.5 pt locked brackets and the progress arc. Line ends are square and corners mitred (No. 12). Dashed pattern for
unknown only: 3 on, 3 off. No shadows, anywhere.

SF Symbols use outline variants at `.regular` weight so they sit with the hairlines (`checkmark.seal`, not
`checkmark.seal.fill`). Status icons keep their fill only inside the tinted badge.

Illustrations: three or four line drawings in our own hand (an exploded part, a construction circle, a flying
machine), 1.5 pt ink, no fills. Leonardo's drawings are public domain but the scans on the board belong to whoever
made them, so we draw our own and trace nothing from the board.

## 6. Motion

`Motion` and `Haptic` stay as they are. One addition: **ink draws on**. The progress arc and the verdict rule
draw with a stroke trim (`Motion.standard`) once, when they appear. Under Reduce Motion they fade in. Nothing on
the camera draws on; overlays still change at most ten times a second.

## 7. Screen by screen

What the Figma prototypes show. Each screen in light and dark; capture over a real camera frame.

### 7.1 Home and scans list (No. 10, 24, 27, 28)

- Masthead: the `LEA` wordmark in EB Garamond, a `label` line under it ("12 SCANS · LAST 24 OCT"). Settings is a
  round outline icon button.
- How it works: three steps (scan, close-ups, report) as a numbered list with a small line drawing each. The
  numbers are real: it is a sequence.
- Each past scan is one row: crop thumbnail (48 pt, radius 6), object words in SF Pro semibold, date in `label`,
  and a verdict tag (tone dot + word, `pillLabel`). Rows are separated by hairlines, not boxed one by one.
- Empty state: the flying-machine drawing, one sentence, the Scan button.

### 7.2 Consent, pairing, settings (No. 08, 29, 30)

Serif screen title, grouped rows on `surface`, section headers in `label`. At most one drawing per screen (the
construction circle on consent). Pairing code digits in `metric`. No new components.

### 7.3 Capture (No. 12-17)

- Every line is chalk over a keyline. No paper colour anywhere on this screen.
- Lock brackets: open corners with square ends and a mitred corner, no rounding (No. 12). Searching: chalk 1.5 pt.
  Locked: azurite 2.5 pt, settles once with `Haptic.lock`. Both over a keyline. The RealityKit box draws the same
  corners.
- Dome dial: an astrolabe (No. 13). Smoke disc, a hairline rim with 12 ticks (one per sector) and a small `N`-style
  mark at sector 0. Empty cells are 1 pt chalk outlines at 55 %, covered cells chalk at 85 %, the next cell azurite
  with an index tick on the rim beside it, all covered cells sage when done. "Here" is a compass needle on the rim
  (No. 14).
- Cue banner: smoke capsule, chalk SF Pro text, a tone disc with a dark glyph. Guide cues use a chalk ring, not a
  fill.
- Counter pills: smoke capsule, `label` numerals ("14 PHOTOS").
- One big button: chalk capsule, ink label.

### 7.4 Report progress (No. 02, 13, 29)

The orb becomes a construction drawing: a hairline circle with an inscribed square (No. 29), and the progress arc
drawn on in ink over it. Failed turns the arc and glyph oxblood. Steps read like a notebook index: `label` numbers,
SF Pro titles, an ink check when done, a small ink spinner while active.

### 7.5 Report (No. 04-06, 09, 18-23, 25)

- Verdict header: `label` eyebrow ("REPORT · 24 OCT 2026 · 14:32"), the verdict words in `heroTitle`, a short ink
  rule that draws on, the reason in SF Pro, meta in `label`. The verdict's tone is in the icon and rule, not in the
  title text.
- Safety card: oxblood hairline frame, 10 % oxblood wash, oxblood title with the triangle. No coloured side bar.
- Problem cards are specimen labels: top row `No. 01` in ink (never the tone: No. 20's red number is the part we
  don't take) and the outlined severity tag; part name in `cardTitle`; issue in the tone colour; crop on the right
  (radius 6, hairline, true colour); ruled rows for where and evidence; confidence as an italic margin note at the
  right ("medium confidence").
- Not inspected and couldn't judge: one list, neutral sepia, dashed-outline badges (eye-slash and dashed square),
  the reason under each part, and the per-photo grouped counts the report already has.
- Next step: ink hairline frame, `label` eyebrow "NEXT STEP", title in `cardTitle`, instruction in SF Pro.
- Limits ("What photos can't tell"): no frame; the title in `annotation`, the lines in sepia SF Pro, like notes at
  the foot of a page.

### 7.6 Problem detail and 3D model (No. 02, 21, 22, 26)

- Header in museum-label order: part (`screenTitle`), issue in its tone, then where, confidence and likelihood.
- The photo crop carries a hairline leader line from the defect box to a short label (No. 22). The box itself is a
  1.5 pt tone-coloured corner set, the same shape as the lock brackets.
- 3D model: the scan in its true colour (principle 5), set on a line-art stage: a hairline ground grid and a
  construction circle under it, ink on paper or pearl on walnut (No. 02, 26). Problem pins are tone-coloured dots with
  leader lines to their `No.` label.

## 8. Accessibility

- Text contrast ≥ 4.5 : 1 for every reading colour in both modes (tables above); `faint` is never used for text
  people need.
- Dynamic Type everywhere off the camera; camera text caps at `.accessibility1`, as now.
- Status is always icon + word + colour; unknown is also dashed; over the camera, states also differ in shape
  (rim tick, stroke weight). Works with Differentiate Without Color.
- EB Garamond ships 400-800 upright and italic, so Bold Text has real weights to move to.
- Margin notes are italic serif, not script, so they stay readable at every size and VoiceOver reads the same words.
- Reduce Motion: draw-ons become fades, as every other movement does now.

## 9. Changes for the Theme.swift PR

After the prototypes are agreed. One PR: `ios/RepairOS/**`, plus the font entries in `ios/Config/Info.plist` and
`ios/THIRD_PARTY_NOTICES.md`, and the L2 line in PLAN.md.

- `Palette`: `accent` and `accentFill` become ink; add `sepia`, `faint`, `brass`, `chalk`, `keyline`, `smoke`,
  `azurite`; replace `cameraAccent` with `azurite`; new values for `canvas`, `surface`, `well`, `hairline` and the
  four tones.
- `Tone.neutral` uses `sepia` instead of `Color.secondary`, so neutral text stays warm.
- `CueBanner.Tone.fill`, `LockBoxSpec.uiColor`, `DomeDial.color(_:)` and the black fills in `StatusPill`,
  `ProgressPill` and `RoundIconButtonStyle` move onto camera tokens.
- `Font`: the mapping in §4; register EB Garamond (upright and italic) in Info.plist (`UIAppFonts`).
- `Radius`: §5. `Symbol`: outline variants.
- `StatusPill` becomes an outlined tag (no tint). `LockBrackets` loses its rounded corners and round caps.
  `DomeDial` gets the rim tick for the next cell and 55 % empty cells.
- New shapes: dashed badge for unknown, keyline stroke helper for camera lines, draw-on modifier.
- `DesignGallery.swift` previews updated so every token shows in light, dark and over a camera frame.

## 10. Open questions

| Question | Owner |
|---|---|
| Add SIL OFL 1.1 (fonts only) to PLAN.md L2? | Owner |
| Who draws the three or four line illustrations? | Design |
| Who gets edit access to the Figma file | Design |
| Website: same codex direction, or its own? Designed after the app design is final | Design |

## 11. Review log

Review 1, 2026-10-24, of the moodboard against this document and the code:

1. Tone text on its own tint failed 4.5 : 1 (four tones): tones moved, tags outlined.
2. The camera refs were all light-on-black: smoke 72 %, keyline 80 % and 2 pt wider, empty cells 55 %.
3. The next dial cell differed by hue alone: rim tick added.
4. No. 11 used red as the brand colour: retired (numbers kept).
5. Caveat dropped for EB Garamond Italic.
6. Line-art or sepia evidence would hide colour defects: principle 5.
7. Dashes were decorative and meaningful at once: unknown only.
8. No. 10 cropped (torn paper, tape); No. 20 captioned (red number).
9. Sourcing on the board overstated: footer corrected, No. 13 credited, No. 28 marked as stock.
