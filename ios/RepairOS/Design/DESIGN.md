# LEA design: the inspector's codex

Draft 3, 2026-10-24, after review 3 (§13). The direction for the iOS app, drawn from the moodboard and written so the
Figma prototypes and the next `Theme.swift` change can be built from it without guessing. Reference numbers (No. 01-30)
point at the [moodboard](https://claude.ai/artifact/NeND4dq9nJpS9FyS1QkpzL) (private until shared). Product truth:
[PRODUCT.md](PRODUCT.md). Tokens in code: [Theme.swift](Theme.swift); capture wiring: [INTEGRATION.md](INTEGRATION.md).

Status: direction agreed → Figma framework built ([file](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u/LEA-%E2%80%94-Inspector-s-Codex-%C2%B7-Product-design-framework?m=auto&t=fFachNvR1GBg0FTj-1)): tokens,
components and screens 01-09 in both modes → **team review** → `Theme.swift` PR → screens. No code changes until the
review settles the design. Snapshots of every page: [ios/figma](../../figma/README.md). Alegreya Sans renders in that
file as it will on the phone. SF Pro renders blank in its exports, so Inter stands in for it; drawn icons stand in for
SF Symbols.

## 1. The idea

LEA reads like Leonardo's notebook used as a working instrument, and Lea is the friend who knows repair holding it.
Iron-gall ink on gesso by day, pearl and brass on ebony by night, and Lea's verdigris wherever she speaks or you act.
The camera never turns into paper; only the ink lines come with you. The report gives printed facts, says how sure it
is in words, and says what Lea couldn't check.

The look comes from line, type and annotation. It never comes from texture.

## 2. Principles

1. **Ink, not texture.** Gesso and ebony are flat tones. No parchment images, paper grain, wood grain, torn edges or
   wax seals. The notebook feeling comes from hairlines, construction circles, leader lines and type (No. 01, 03).
2. **Red means danger.** Oxblood (light) and vermilion (dark) mark safety problems and nothing else (No. 16).
3. **Judgement is a word.** What the app measured or saw is a printed fact. Confidence and likelihood are judgement:
   plain upright words at the right of the card, never a number and never a script or italic voice (I6).
4. **Unknown is a construction line.** Anything not inspected or not judged is drawn dashed, the way an unfinished
   sketch is drawn. Anything judged is inked solid. This is I3 and I4 made visible: `not_inspected` can never look
   like `ok`. Dashes mean unknown and nothing else, so decorative construction lines are drawn solid.
5. **Evidence keeps its colour.** Photos, crops and the 3D scan are never tinted, toned or turned into line art:
   colour is evidence (scorch marks, corrosion, "colour doesn't match"). Only our marks around them are inked.
6. **The camera gets line art only.** Chalk lines with a keyline, smoked fills, no paper, no blur, no shadows, no
   animated gradients, ≤ 10 changes a second (the rules in INTEGRATION.md stand).
7. **One action per screen, in verdigris.** Lea's colour marks the one thing to do on a screen, and Lea herself.
   Nothing else is that green: never a status, never decoration.
8. **Colour is never the only cue.** Every tone comes with an icon and a word; over the camera, a shape.
9. **Lea is there.** Her mark appears wherever she listens or speaks, her words are captions rather than chat
   bubbles, and the report keeps what she said when it landed.

## 3. Colour

Sampled from the moodboard's own pigments and materials (No. 03, 04, 07, 16, 26): gesso, iron-gall ink, verdigris
(the green brass grows, and Leonardo's own pigment), leaf, ochre, oxblood, iris and brass. Every text colour passes
4.5 : 1 on canvas, card and its own tint, and every status pair stays apart for deuteranopia and protanopia (CIELAB ΔE
on Machado-simulated colours).

### Gesso (light) and Ebony (dark)

| Token | Light | Dark | Use | Contrast light / dark |
|---|---|---|---|---|
| `canvas` | `#F3F2EE` gesso | `#111417` ebony | Screen background | |
| `surface` | `#FFFFFF` | `#1B1F23` | Cards and grouped rows | |
| `well` | `#E9E8E3` | `#252A2F` | Inputs, thumbnails while loading | |
| `hairline` | ink at 12 % | brass at 28 % | 0.5 pt card edges and ruled rows | |
| `ink` | `#1A2023` iron-gall | `#EDEAE2` pearl | Primary text | 14.7 / 13.8 |
| `secondary` | `#555E66` graphite | `#A3ABB1` | Secondary text, the neutral tone | 5.9 / 7.1 |
| `faint` | `#8B9399` | `#6E767D` | Placeholders and disabled only (never for text people must read) | 2.8 / 3.6 |
| `accent` | `#0F514B` verdigris | `#8FD3C7` | The one action, links, Lea's mark and name | 8.2 / 9.7 |
| `onAccent` | `#FFFFFF` | `#0F1A19` | Label on an accent fill | 9.1 / 10.4 on accent |
| `brass` | (not used) | `#C2A468` | Dark-mode hairlines and rules | 7.8 on ebony |

### Status tones

| Tone | Light | Dark | Means | Contrast light / dark | On its own tint |
|---|---|---|---|---|---|
| `good` leaf | `#3A7536` | `#B5D58A` | Verdict "No visible problems", dome done | 5.0 / 10.1 | 4.9 / 7.0 |
| `caution` ochre | `#8A5F00` | `#E9B24F` | Severity `functional`, fixable cues | 5.0 / 8.6 | 4.9 / 6.2 |
| `danger` oxblood / vermilion | `#8E2316` | `#F0785C` | Severity `safety`, blocking cues, failures | 7.8 / 6.0 | 7.4 / 4.7 |
| `cosmetic` iris | `#4F4697` | `#B2A7EA` | Severity `cosmetic` | 7.1 / 7.6 | 6.8 / 5.6 |
| `neutral` graphite | `#555E66` | `#A3ABB1` | `not_inspected`, `insufficient_evidence` (with the dashed form) | 5.9 / 7.1 | 5.7 / 5.3 |

Contrast is the worst of canvas and card. A tint is the tone at 10 % (light) or 16 % (dark) over the card; tags are
outlined, so the safety card is the one tinted surface.

Separation of the status tones, worst pair across normal, deuteranopia and protanopia vision (CIELAB ΔE76):

| | Today's theme | Draft 2 (cream) | This palette |
|---|---|---|---|
| Light | ΔE 15.8, but good 3.1, caution 3.4, danger 4.3 : 1 as text | 13.3 | 14.3 (good vs danger, deuteranopia) |
| Dark | 12.6 | 19.3 | 16.2 (good vs danger, deuteranopia) |

Verdigris sits apart from neutral graphite by ΔE 9.4 or more under all three visions; it never marks a status, and
the neutral tone always comes with its dashed form.

### Camera (same in both modes)

| Token | Value | Use |
|---|---|---|
| `chalk` | `#F4F2EC` | Every line and label over the camera |
| `keyline` | `#0E1214` at 80 % | A stroke 2 pt wider under each chalk line (1 pt each side), so it reads on bright and dark scenes without a shadow (10.2 : 1 on white) |
| `smoke` | `#0E1214` at 72 % | Solid fill behind the cue, pills, dial disc and round buttons |
| `next` | `#8FD3C7` verdigris | The next dome cell and the locked brackets. Lea's colour, so it is the same green as on the app screens |

Measured over a white scene, the worst case: chalk text on smoke 6.8 : 1, empty dial cells at 55 % chalk 3.3 : 1.

Cue colours over the camera reuse the dark status set. The glyph on a cue disc is `#0E1214` (11.5 : 1 on leaf, 9.8 on
ochre, 6.8 on vermilion). Verdigris is at least ΔE 34 from every cue colour under all three visions. Verdigris and chalk
are close in lightness (1.7 : 1), so verdigris is never the only thing marking a state: the next cell also gets a rim
tick, the locked brackets a heavier stroke.

### Rules

- Brass never appears in light mode and never on a status element.
- Verdigris is Lea's: the one action, links, her mark, her name in captions, and "next" over the camera. Never a
  status, never a fill for decoration.
- No literal colours outside `Theme.swift`. Today `CueBanner.Tone.fill` and `LockBoxSpec.uiColor` hold hex values;
  the Theme PR moves them onto camera tokens.

## 4. Type

Three voices. No mono labels, no italic serif, no all-caps tracking.

| Voice | Face | Licence | Where |
|---|---|---|---|
| Display | Alegreya Sans, Bold and ExtraBold | SIL OFL 1.1, bundled | Screen titles, verdict, section titles, part names, the `lea` wordmark |
| Reading | SF Pro (system) | Apple system font, not bundled | Body, instructions, reasons, buttons, cues, captions |
| Label | SF Pro semibold, sentence case | Apple system font | Field names, tags, dates, counters; numbers with `monospacedDigit()` |

Why Alegreya Sans: a humanist sans with a calligraphic, Renaissance-hand rhythm, so it carries the notebook without
reading as a book; it is warm and plain enough to sound like Lea; it has nine weights and italics, so Bold Text has
somewhere to go; and it renders in the Figma file, so the team reviews the real face. Considered and left out: EB
Garamond (review 3: the cream, serif and mono-label set reads as a generated report), Optima (the best lineage, from
Florentine inscriptions, but it does not render in Figma, so the team could not review it) and SF Pro alone (nothing
of Lea in it).

Token mapping (every custom face scales with `Font.custom(_:size:relativeTo:)`):

| Token | Today | New |
|---|---|---|
| `heroTitle` | SF Rounded bold, largeTitle | Alegreya Sans Bold 34, relative to `.largeTitle` |
| `screenTitle` | SF Rounded bold, title | Alegreya Sans Bold 28, relative to `.title` |
| `sectionTitle` | SF Rounded semibold, title3 | Alegreya Sans Bold 22, relative to `.title3` |
| `cardTitle` | SF Pro headline | Alegreya Sans Bold 19, relative to `.headline` |
| `buttonLabel` | SF Rounded semibold | SF Pro semibold, `.headline` |
| `pillLabel` | SF Rounded semibold footnote | SF Pro semibold `.caption`, sentence case |
| `cue` | SF Rounded semibold callout | SF Pro semibold `.callout` (stays sans for legibility over the camera) |
| `metric` | SF Rounded semibold title2 | SF Pro semibold `.title2`, `monospacedDigit()` |
| `wordmark` | SF Rounded bold 30, fixed | Alegreya Sans ExtraBold 34, fixed, tracking -1 %, always lowercase `lea` |
| `label` (new) | | SF Pro semibold `.footnote`, sentence case |
| `judgement` (new) | | SF Pro `.subheadline` in `secondary`: confidence and likelihood words |

- Body text stays SF Pro at the system text styles, so Dynamic Type, Bold Text and accessibility sizes behave as
  they do now.
- Alegreya Sans has an ordinary x-height, so display sizes need no offset over their text style.
- No handwriting font, no script, no italic asides: they dress a model's judgement as a person's writing and are the
  hardest voice to read for dyslexic and low-vision readers.
- **Licence check needed.** PLAN.md L2 lists Apache-2.0, MIT, BSD, CC-BY and the Apple Sample Code License. SIL OFL
  1.1 is a permissive font licence but is not on that list. The Theme PR adds "SIL OFL 1.1 for fonts" to L2 (owner
  sign-off) and Alegreya Sans to `ios/THIRD_PARTY_NOTICES.md`.

## 5. Line, shape and space

| | Today | New |
|---|---|---|
| Card radius | 22 | 12: labels pinned on a board, not bubbles |
| Medium / small / thumb radius | 14 / 8 / 12 | 10 / 6 / 6 |
| Primary button | 56 pt capsule, iris | 56 pt capsule, verdigris |
| Card edge | 0.5 pt hairline | 0.5 pt hairline; dark mode uses brass |
| Inside a card | grey wells | 0.5 pt ruled rows between fields (No. 20) |
| Tags | tone text on a tinted capsule | 0.5 pt tone outline, tone icon, tone word, no tint (No. 24) |
| Problem numbers | a filled disc | a 26 pt disc outlined in the severity tone, the number in Alegreya Sans, matching the marker on the 3D scan |
| Spacing | `Spacing` | Unchanged |

Line weights: 0.5 pt rules; 1 pt construction lines and empty dome cells; 1.5 pt searching brackets, drawings and
Lea's mark; 2.5 pt locked brackets and the progress arc. Line ends are square and corners mitred (No. 12). Dashed
pattern for unknown only: 3 on, 3 off. No shadows, anywhere.

**Lea's mark.** A 38 pt ring with 12 ticks inside the rim and a centre point (the astrolabe, No. 13, 14), always in
verdigris. The ticks are her voice level: your level while she listens, hers while she speaks; while she thinks,
three lit ticks walk round. Idle ticks are at 30 %. It is the talk button on Home, the left end of the Ask Lea bar,
and the avatar beside her words in the report. The wordmark is `lea` followed by her centre point.

SF Symbols use outline variants at `.regular` weight so they sit with the hairlines (`checkmark.seal`, not
`checkmark.seal.fill`).

Illustrations: three or four line drawings in our own hand (an exploded part, a construction circle, a flying
machine), 1.5 pt ink, no fills. Leonardo's drawings are public domain but the scans on the board belong to whoever
made them, so we draw our own and trace nothing from the board.

## 6. Motion

`Motion` and `Haptic` stay as they are, with `reveal`, `pop`, `rise` and `stagger` from the report stage (PR #13).
Two additions:

- **Ink draws on.** The progress arc draws with a stroke trim (`Motion.standard`) once, when it appears.
- **Lea's ticks follow her voice**, at most ten updates a second.

Under Reduce Motion the arc fades in and the ticks hold still. Nothing on the camera draws on.

## 7. Screen by screen

What the Figma prototypes show. Each screen in light and dark; capture over a camera stand-in.

### 7.1 Home and scans list (No. 10, 24, 28)

- Masthead: the `lea` wordmark with her verdigris point, and Settings as a round outline icon button. No tagline, no
  rule under it.
- First run: "Find what's broken", the line from `main` ("Walk around it with your phone. Lea shows you what looks
  wrong, and where.") and the four steps from `main` as a numbered list (lock on, walk around it, a few close-ups, read
  the report). The numbers are real: it is a sequence.
- Each past scan is one row: crop thumbnail (48 pt, radius 6), object words in SF Pro semibold, date and photo count
  in `secondary`, and a verdict tag. Rows are separated by hairlines, not boxed one by one.
- Bottom: the talk bar. "What happened? (optional)" with the mic, Start scan in verdigris, and Lea's mark as the
  button that opens the conversation (7.7).

### 7.2 Consent, pairing, settings (No. 08, 29, 30)

Alegreya Sans screen title, grouped rows on `surface`, section headers as labels. At most one drawing per screen (the
construction circle on consent). Pairing code digits in `metric`. No new components.

### 7.3 Capture (No. 12-17)

- Every line is chalk over a keyline. No paper colour anywhere on this screen.
- Lock brackets: open corners with square ends and a mitred corner, no rounding (No. 12). Searching: chalk 1.5 pt.
  Locked: verdigris 2.5 pt, settles once with `Haptic.lock`. Both over a keyline. The RealityKit box draws the same
  corners.
- Dome dial: an astrolabe (No. 13). Smoke disc, a hairline rim with 12 ticks and a small mark at sector 0. Empty cells
  are 1 pt chalk outlines at 55 %, covered cells chalk at 85 %, the next cell verdigris with an index tick on the rim
  beside it, all covered cells leaf when done. "Here" is a compass needle on the rim (No. 14).
- Cue banner: smoke capsule, chalk SF Pro text, a tone disc with a dark glyph. The cue is what Lea says aloud.
- Counter pills: smoke capsule, chalk numerals ("14 photos").
- One big button: chalk capsule, ink label.

### 7.4 Report progress (No. 02, 13, 29)

The orb is a construction drawing: a hairline circle with an inscribed square (No. 29), the progress arc drawn on in
ink over it. Failed turns the arc and glyph oxblood. Steps read like a notebook index: plain numbers, SF Pro titles,
an ink check when done, a small ink spinner while active. On `main` the waiting state is the scan turning on the dark
stage; the Theme PR keeps that and draws the stage's ground as below (7.6).

### 7.5 Report (No. 04-06, 09, 18-23, 25)

In this order:

1. **Verdict.** The verdict in `heroTitle`, the reason in SF Pro, then one meta line: your own words in quotes, the
   object, photos, date. No eyebrow above it and no rule under it. The verdict's tone is in the tags and icons, not in
   the title.
2. **Safety card.** Oxblood hairline frame, 10 % oxblood wash, oxblood title with the triangle. No side bar.
3. **What Lea said.** Her mark (speaking) and the plain-words summary she spoke when the report landed, unboxed.
4. **Most likely problems.** The 3D view with numbered pins, then problem cards as specimen labels: the numbered
   disc and the outlined severity tag; part name in `cardTitle`; issue in the tone colour; crop on the right (radius
   6, hairline, true colour); ruled rows for where and evidence; confidence as a plain word at the right.
5. **What you suspected.** Your words in quotes, then "What Lea found" with its badge (photos agree, not seen in the
   photos, could not check) and the backend's note. Your guess only steered where she looked; it is never a finding.
6. **Next step.** Ink hairline frame, a "Next step" label, title in `cardTitle`, instruction in SF Pro.
7. **Fix it.** The part, offers as ruled rows (title, seller, price as the backend gives it) and the replacement
   guide. Every word comes from the backend; nothing is invented.
8. **Cosmetic.** One row per cosmetic finding.
9. **What Lea couldn't check.** Not inspected and could-not-judge in one card: a one-sentence summary, the dashed
   rows with their reasons, Continue scanning, and "Show all".
10. **What photos can't tell.** No frame; a quiet title and the lines in `secondary`, like notes at the foot of a page.
11. **Ask Lea.** The bar at the bottom: hold to ask about this scan. The answer is spoken and captioned.

### 7.6 Problem detail and 3D model (No. 02, 21, 22, 26)

- Header in museum-label order: "Problem 1 of 3", part (`screenTitle`), issue in its tone, then where, confidence and
  likelihood as words.
- The photo crop carries a hairline leader line from the defect box to its number (No. 22). The box itself is a
  1.5 pt tone-coloured corner set, the same shape as the lock brackets.
- 3D model: the scan in its true colour (principle 5), set on a line-art stage: a hairline ground grid and a
  construction circle under it, ink on gesso or brass on ebony (No. 02, 26). Problem pins are tone-coloured dots with
  leader lines to their number. The dark stage from PR #13 keeps its layout but loses the radial gradient and the
  violet: flat ebony, the construction-circle ground, verdigris for selection.

### 7.7 Talk to Lea (new)

The conversation before a scan (`HomeConversationSheet` on `main`). Title "Lea" and Done. One line: "Tell Lea what it
is and what's wrong. She'll work out the exact product before you scan." The product brief as a card with ruled rows
(what it looks like, known issue, sources). Two quiet buttons: the label photo and Start scan. Then the captions, both
sides, Lea's name in verdigris. At the bottom, Lea's mark at 88 pt, listening, with one line of help.

## 8. Accessibility

- Text contrast ≥ 4.5 : 1 for every reading colour in both modes (tables above); `faint` is never used for text
  people need.
- Dynamic Type everywhere off the camera; camera text caps at `.accessibility1`, as now.
- Status is always icon + word + colour; unknown is also dashed; over the camera, states also differ in shape
  (rim tick, stroke weight). Works with Differentiate Without Color.
- Alegreya Sans ships 100-900 upright and italic, so Bold Text has real weights to move to.
- Lea's words are captions as well as speech; VoiceOver reads the same words she says.
- Reduce Motion: draw-ons become fades and Lea's ticks hold still.

## 9. Changes for the Theme.swift PR

After the prototypes are agreed. One PR: `ios/RepairOS/**`, plus the font entries in `ios/Config/Info.plist` and
`ios/THIRD_PARTY_NOTICES.md`, and the L2 line in PLAN.md. Build on `main`'s Theme.swift, which already has the stage
tokens, `cautionText` and the report motion from PR #13.

- `Palette`: `accent` becomes verdigris and `accentFill` folds into it; add `onAccent`, `secondary`, `faint`, `brass`,
  `chalk`, `keyline`, `smoke`, `next`; replace `cameraAccent` with `next`; new values for `canvas`, `surface`,
  `well`, `hairline` and the four tones. `cautionText` goes: the new ochre passes 4.5 : 1 as text.
- Stage: `stageCenter` and `stageEdge` become one flat ebony; `stageGlass` stays for controls.
- `Tone.neutral` uses `secondary` instead of `Color.secondary`.
- `CueBanner.Tone.fill`, `LockBoxSpec.uiColor`, `DomeDial.color(_:)` and the black fills in `StatusPill`,
  `ProgressPill` and `RoundIconButtonStyle` move onto camera tokens.
- `Font`: the mapping in §4; register Alegreya Sans (Bold, ExtraBold and their Bold Text steps) in Info.plist
  (`UIAppFonts`).
- `Radius`: §5. `Symbol`: outline variants.
- `StatusPill` becomes an outlined tag (no tint). `LockBrackets` loses its rounded corners and round caps.
  `DomeDial` gets the rim tick for the next cell and 55 % empty cells.
- New views: `LeaMark` (four states), the dashed badge for unknown, a keyline stroke helper for camera lines, the
  draw-on modifier.
- `DesignGallery.swift` previews updated so every token shows in light, dark and over a camera frame.

## 10. Open questions

| Question | Owner |
|---|---|
| Add SIL OFL 1.1 (fonts only) to PLAN.md L2, for Alegreya Sans? | Owner |
| Who draws the three or four line illustrations? | Design |
| Who gets edit access to the Figma file | Design |
| The report stage from PR #13 against 7.5 and 7.6: one pass in the Theme PR | App |
| ~~Website: same codex direction, or its own?~~ The same direction: §11 | Decided 2026-10-24 |

## 11. Website

The site uses this direction, not one of its own (§10). It lives in `website/` (stream `website`,
`areas/website.md`).

- **Colour.** The §3 tokens, light (gesso) and dark (ebony) by `prefers-color-scheme`. The same rules apply:
  red only for safety, brass only in dark mode, verdigris only for Lea and the one action, no colour outside the token
  block at the top of `styles.css`.
- **Type.** Alegreya Sans 700 and 800 from Google Fonts for display (the §10 licence question covers this too). Body
  and labels are the system font: `-apple-system` (SF Pro on Apple devices), then Segoe UI or Helvetica. No monospace
  labels; Inter is not used.
  - Headline: `clamp(2.6rem, 5vw, 4.6rem)`, line height 1.03, at most 15 characters per line.
  - Section titles: `clamp(2rem, 3.4vw, 3.1rem)`.
  - Reading text: 17 px with line height 1.55, and lines no longer than 34 rem.
- **Layout.** Content is at most 1248 px wide, with 96, 48 and 20 px gutters and breakpoints at 1080 and 760 px.
  - Sections are separated by a 0.5 px rule, not by coloured bands. Section headings have no eyebrow above them;
    the nav already names the sections.
  - Numbers appear only for a real sequence: the method steps and the problem numbers.
- **Figures.** Real screens only, set flat with a 0.5 px edge and a 34 px corner. There is no device frame, shadow
  or tilt. Each figure has a `Fig. n` caption in the label voice.
  - Until the Theme.swift PR lands, the screens are the Figma exports and the captions say "design prototype".
  - Light and dark versions swap with `<picture>`. Evidence slots stay empty, as in §7.
- **Components.**
  - **Primary action:** the verdigris capsule, at least 52 px tall, one per view. Everything else is an underlined
    text link.
  - **Lea:** her mark beside anything she says.
  - **Report examples:** the specimen label (§7.5) and the outlined tag (§5).
  - **Unknown:** a dashed box. Dashes mean unknown and nothing else.
  - **Features:** a ledger of ruled rows, not cards. The limits are set as notes at the foot of a page.
- **Motion.** Smooth scroll and a hover fade on the button, nothing else. Reduced Motion turns smooth scroll off.
  No JavaScript.
- **Copy.** Only what the product does today, in the report's plain words. Screens are labelled as prototypes.
  Grain is never named. There are no numbers, customers or quotes we cannot show.

## 12. Rules for generated design

For people and coding agents, web and iOS. Load this section into an agent's context before it designs
anything.

**The three generated looks.** Generated interfaces fall into the same few looks whatever the subject:

1. A warm cream ground, a high-contrast serif display face and a terracotta or red accent.
2. Near-black with one neon accent and glowing edges.
3. Editorial hairlines, an italic serif for asides and small tracked mono labels in caps.

Draft 2 of this document was 1 and 3 at once, which is why it read as a generated report (review 3). If someone could
guess the look from the category alone ("repair app", "AI report"), rework it.

**Banned.** None of these ship:

- Purple-to-blue or any decorative gradient, glow blobs, blur used as decoration, floating 3D shapes,
  corporate-Memphis figures, stock illustration.
- A cream or parchment ground with a serif display face and tracked mono labels.
- Small uppercase tracked labels ("REPORT · 24 OCT 2026"); an eyebrow over a heading; a short accent rule under one.
- "No. 01" catalogue numbering, or leading zeros, where the numbers are not a sequence the reader uses.
- Italic or script type for asides, confidence or anything the model judged.
- Chat bubbles for Lea; she speaks in captions.
- A grid of boxed cards where ruled rows would do; an icon in a circle on every card; emoji or Unicode glyphs
  standing in for icons.
- Drop shadows, or any elevation (§5: there is no elevation scale, everything is flat on hairlines).
- Stat counters, logo walls or testimonials we cannot back up.

**Tokens are fixed.** Use only the hex values in §3, the faces and sizes in §4 and §11, the radii and line weights
in §5 and the `Spacing` scale. A brief, prompt or PR that needs a new value adds it here first.

**Start from the product, not the category.** Before choosing a colour or a face, write down what only this product
has: Lea's voice, the coverage dome, the honest unknowns, the user's own words. Draft 3's new pieces (Lea's mark, the
captions, "What you suspected", "What Lea couldn't check") all come from `main`, not from a template.

**Visual anchors.** Design from the Figma screens and real captures, not from a text description. A new screen
starts from the nearest existing frame.

**See the face before choosing it.** Set the candidate faces on real copy, in both modes, in the tool the team
reviews in. A face that cannot render there cannot be reviewed.

**Measure colour before committing.** Every token goes through contrast (text ≥ 4.5 : 1 on canvas, card and its own
tint) and colour-blind separation (ΔE on Machado-simulated deuteranopia and protanopia) before it reaches Figma.

**The deletion loop.** On every first draft, remove the three most obvious generated-looking choices, then check
the layout still holds on type, rules and whitespace alone. If it doesn't, the structure is wrong; adding
decoration won't fix it. Record what was removed in §13.

**Two loops, then tighten.** At most two design iterations, then the work moves to code: contrast, spacing and copy.

**Checks.**

- Every text and background pair passes 4.5 : 1, in both modes (§3, §8). The proprietary guard passes.
- **iOS:** Xcode Accessibility Inspector on every screen, and SwiftLint.
- **Web:** a contrast check, and a look at 390, 768 and 1440 px in both modes.

**iOS: Apple's Human Interface Guidelines, within the codex.**

- **Containers.** Build with native SwiftUI containers (`NavigationStack`, `List`, `ScrollView`, `Grid`, `Form`)
  and system controls. Don't wrap a web-style card template around them.
- **Type and icons.** Body text uses SF Pro at Dynamic Type text styles (`.body`, `.headline`, …). Alegreya Sans
  scales with `relativeTo:`. Icons are SF Symbols, using the outline variants (§5).
- **Touch and gestures.** Tap targets are at least 44 × 44 pt, content stays inside the safe area, and there are
  no gestures the system doesn't already use.
- **Colour.** Theme.swift's semantic tokens replace `Color(.systemBackground)` and `Color(.label)`. The gesso and
  ebony palette is the brand, but each token adapts to light, dark and Increase Contrast the way a system colour
  does. Screens never use a raw colour.
- **Materials.** System materials appear only where the system draws them: navigation bars, sheets and menus. No
  custom glass or blur, and nothing over the camera (§2.6).
- **Radii.** 12 pt for cards (`RoundedRectangle(cornerRadius: 12)`), as in §5.

## 13. Review log

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

Review 2, 2026-10-24, the website (§11) and the rules for generated design (§12):

1. The first website draft was generic: an iris gradient band, a purple glow behind the phone, a grid of six
   icon-in-a-circle cards and an eyebrow over every heading. It was thrown away and rebuilt on this document.
2. Deletion loop on the second draft: the eyebrows over every section, and the numbers on the report rules (they
   are not a sequence), were removed. The layout held on rules and whitespace alone.
3. The step figures were mismatched (a problem-detail screen under "Close-ups"), so each figure now shows its own step.

Review 3, 2026-10-24, of the Figma file: "it looks like a Claude-generated report, not like Lea, and the font throws
me off". The moodboard stays; its rendition changed.

1. Draft 2 was two of the generated looks at once (§12): cream ground, serif display, italic margin notes and tracked
   mono labels. Cream and walnut brown became gesso and ebony; brown ink became iron-gall; ink buttons became
   verdigris, which is also Lea's colour.
2. Type: EB Garamond, JetBrains/SF Mono labels and the italic margin voice were dropped for Alegreya Sans and SF Pro
   only. Optima was the first choice but does not render in Figma.
3. Deletion loop on the report: the eyebrow over the verdict, the accent rule under it and the "No. 01" labels were
   removed; problem numbers became discs that match the 3D markers. The layout held.
4. Lea was missing. The screens were drawn from an older app; `main` now has her conversation, her voice on the
   report, "What you suspected", "What Lea couldn't check" and Fix it. Added: Lea's mark, captions, the talk bar, the
   Ask Lea bar, those three cards, and screen 09.
5. Azurite over the camera became verdigris (ΔE ≥ 34 from every cue colour), so Lea has one colour everywhere.
6. Status tones re-measured: light worst pair 14.3 (was 13.3), dark 16.2 (was 19.3, still above today's 12.6).
