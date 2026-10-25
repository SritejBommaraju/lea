# LEA design: violet and frosted glass

Final visual direction, synchronized from Figma. The latest user decisions replace the green/brass palette and
flat-only surfaces with violet actions, lavender neutrals and restrained glass. Product truth remains in
[PRODUCT.md](PRODUCT.md); implementation tokens are in [Theme.swift](../../ios/RepairOS/Design/Theme.swift), and capture wiring is in
[INTEGRATION.md](../../ios/RepairOS/Design/INTEGRATION.md). Historical reviews are retained in §13.

**Design handoff:** [final light/dark review](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u?node-id=95-1464),
[component regression board](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u?node-id=86-157), and fresh
[Figma exports](figma/README.md). Resolved token values are in [palette.json](UXHarness/palette.json).
This branch documents the finalized design; production SwiftUI implementation remains a follow-up (§9).

Alegreya Sans is the display face in Figma. Inter stands in for SF Pro in exports; named vector icons stand in for
SF Symbols. Photo and scan slots are explicitly placeholders, never invented evidence. The static exports do not
demonstrate motion: §6 specifies its triggers, timing and accessibility alternatives.

**Validation:** the executable [UX harness](UXHarness/README.md) passes 1,153 Figma assertions across six layouts.
The 30 tested semantic text pairs pass 4.5:1, with a 4.79:1 minimum; fixture text is at least 5.75:1. Native Dynamic
Type, VoiceOver and uncoached scan-to-report checks remain NOT RUN; use the [device protocol](UXHarness/native-validation.md).
Final visual direction does not mean A7 has passed.

## 1. The idea

LEA is a working instrument with a friend who knows repair beside you. Violet actions, cool lavender by day and
deep purple by night give it a modern character. Frosted controls add depth; line drawings, the coverage dial and
evidence markers retain the product's own identity. The report states what was seen, how sure it is in words and
what could not be checked.

The wow factor comes from seeing your actual object become understandable: the scan fills in, the lock settles,
Lea responds to your voice and a chosen finding points back to its evidence. Motion makes these relationships
visible. Texture, fabricated evidence and decorative activity do not carry the interface.

## 2. Principles

1. **Quiet surfaces, clear depth.** Lavender and deep-violet canvases, crisp type and restrained frosted controls.
   No paper texture, ornamental glow or a glass box around every section.
2. **Red means danger.** Danger colour and the warning symbol identify safety issues. Purple is the action colour.
3. **Judgement is a word.** Confidence and likelihood are plain words, never an uncalibrated number or an imitation
   of someone's handwriting (I6).
4. **Unknown stays visibly unknown.** Not inspected and could not judge use explicit words and dashed treatment.
   Neither can look like a successful inspection (I3/I4).
5. **Evidence keeps its colour.** Photos, crops and scans are never tinted, toned or turned into line art. Marks
   sit around evidence without changing it.
6. **The camera stays legible.** Chalk with keylines, smoke fills, clear shapes and semantic updates at most ten
   times a second. Glass, moving backgrounds and decorative effects belong off the live camera.
7. **One primary action, in violet.** Lea's mark and actions share the violet family. Cosmetic status uses a quieter
   plum with an explicit label; colour alone never implies an action or a finding.
8. **Meaning survives without colour.** Every state has a label; safety has a warning symbol and unknown has its
   dashed form. Over the camera, states differ in shape too.
9. **Lea is there.** Her mark appears while she listens or speaks, with captions for her actual words. The written
   report summary is separate. Nothing animates as though she is listening when she is not.
10. **Motion creates the wow factor.** Give the object, the lock, coverage, Lea's voice and selected evidence
    purposeful moving parts. One focal movement at a time; respond immediately to input and let motion settle.
    Keep reading and safety available throughout. Every moving part has a trigger, an end or stop condition and a
    Reduce Motion alternative (§6).

## 3. Colour

The finalized palette is violet, related to the earlier cosmetic purple. The action colour is saturated in light
mode and lavender in dark mode; cosmetic status is a quieter plum. Values below match the live Figma variables
and [resolved export](UXHarness/palette.json). Semantic IDs remain linked across existing screens and components.

### Lavender (light) and Violet night (dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `canvas` | `#F3F1FA` | `#12101B` | Screen background |
| `surface` | `#FBFAFF` | `#211D30` | Opaque reading and safety surfaces |
| `well` | `#E9E4F5` | `#2C263D` | Inputs and thumbnail placeholders |
| `hairline` | `#746293` at 18% | `#C7B5F1` at 22% | Rules and subtle edges |
| `ink` | `#211C32` | `#F5F1FF` | Primary text |
| `secondary` | `#625B73` | `#BEB5D0` | Secondary text and neutral labels |
| `faint` | `#8B9399` | `#6E767D` | Disabled content only; never essential text |
| `accent` | `#6650CC` | `#C2B1FF` | Primary action, links, selection and Lea |
| `onAccent` | `#FBFAFF` | `#21143B` | Label on an accent fill |
| `rule` | `#211C32` | `#BFB2D8` | Construction lines; replaces brass |
| `glass/fill` | White at 76% | `#302844` at 82% | Frosted controls and contained cards |
| `glass/edge` | White at 92% | `#DDD1FF` at 24% | Restrained glass outline |

### Status tones

| Tone | Light | Dark | Meaning |
|---|---|---|---|
| `good` | `#3A7536` | `#B5D58A` | No visible issues in an inspected area; covered dome |
| `caution` | `#8A5F00` | `#E9B24F` | Functional issue or capture caution |
| `danger` | `#8E2316` | `#F0785C` | Safety risk, blocking cue or failure |
| `cosmetic` | `#75638C` | `#C2B3DB` | Cosmetic finding |
| `neutral` | `#625B73` | `#BEB5D0` | Unknown states and evidence metadata |

All 30 tested semantic text pairs (seven reading tones on canvas/surface plus onAccent on accent, in both modes)
pass 4.5:1. The minimum is cosmetic on the light canvas at 4.79:1. The old palette's colour-blind ΔE measurements
do not apply to this revision; no new separation score is claimed. Labels, warning symbols, dashed unknowns and
shape cues preserve meaning without relying on hue. Do not use `faint` for content the person needs to read.

### Glass material

`Material/Frosted glass` is the shared Figma effect style: 24 px background blur; white inner highlight at 22%,
offset (0, 1), no blur; violet shadow RGB (0.12, 0.07, 0.24) at 7%, offset (0, 6), radius 18. The fill and edge
use the semantic glass variables above. The highlight and shadow describe the surface boundary; they do not
animate independently.

Use it on quiet controls, app icon buttons, problem cards, brief cards, floating bottom bars, pinned toolbars and
selected grouped panels. Long reading surfaces and safety notices remain opaque. The tested glass controls sit
over the known app canvas; arbitrary photographic backgrounds have not been validated. Native Reduce Transparency
uses `surface` with an opaque edge and no backdrop blur. Keep text and icons at full opacity.

### Camera (same in both modes)

| Token | Value | Use |
|---|---|---|
| `chalk` | `#F4F2EC` | Lines and labels over the camera |
| `keyline` | `#0E1214` at 80% | Stroke 2 pt wider under chalk |
| `smoke` | `#0E1214` at 72% | Cue, counters, dial and round controls |
| `next` | `#C2B1FF` | Next cell and locked brackets, with shape cues |
| `stage` | `#12101B` | Flat scan stage in both appearances |

The next cell has a rim tick and locked brackets use a heavier stroke. Camera updates show actual capture state;
there is no animated glass, decorative shimmer or invented progress. Evidence pixels remain unchanged.

### Rules

- Colour values live in semantic tokens. Screens never invent their own hues or material constants.
- Violet marks action and selection; an explicit cosmetic label distinguishes a status in the same colour family.
- Glass is a material for specific surfaces, not a wash applied over the whole screen.
- New text/background combinations, opacity changes and photographic backdrops need contrast validation.
- The old green/brass palette is historical (§13), not an alternative theme.

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
| `number` (new) | | Alegreya Sans Bold 15, relative to `.subheadline`: problem numbers and step numbers |
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
| Primary button | 56 pt capsule, iris | 56 pt capsule, violet fill and onAccent label |
| Card edge | 0.5 pt hairline | Reading surfaces use hairline; glass uses the shared edge token |
| Inside a card | grey wells | 0.5 pt ruled rows between fields (No. 20) |
| Tags | tone text on a tinted capsule | Read-only text; safety uses warning icon and danger tone, unknown alone has a dashed outline |
| Problem numbers | a filled disc | one `Marker`: a 26 pt disc on `surface`, outlined 1.5 pt in the severity tone, the number in `number`. The same mark on the card, the 3D stage and the photo leader |
| Headings | | Two levels only: `sectionTitle` for a section, `cardTitle` for an item in it. No label above a heading |
| Boxes | | Safety notices, problem/brief cards, controls and grouped lists get a defined surface. Other content remains a section on the canvas |
| Navigation | custom rows | 44 pt rows; Back, Done and Skip in the tint (violet), chalk over the stage |
| Spacing | `Spacing` | Unchanged |

Line weights: 0.5 pt rules; 1 pt construction lines and empty dome cells; 1.5 pt searching brackets, drawings and
Lea's mark; 2.5 pt locked brackets and the progress arc. Line ends are square and corners mitred (No. 12). Dashed
pattern for unknown only: 3 on, 3 off. Only the shared glass material uses the highlight and shadow in §3.

**Lea's mark.** A 38 pt ring with 12 ticks inside the rim and a centre point (the astrolabe, No. 13, 14), always in
the violet action accent. The ticks are her voice level: your level while she listens, hers while she speaks; while she thinks,
three lit ticks walk round. Idle ticks are at 30 %. It is the talk button on Home, the left end of the Ask Lea bar,
and the avatar beside her words in the report. The wordmark is `lea` followed by her centre point.

SF Symbols use outline variants at `.regular` weight so they sit with the hairlines (`checkmark.seal`, not
`checkmark.seal.fill`).

Illustrations: three or four line drawings in our own hand (an exploded part, a construction circle, a flying
machine), 1.5 pt ink, no fills. Leonardo's drawings are public domain but the scans on the board belong to whoever
made them, so we draw our own and trace nothing from the board.

## 6. Motion and moving parts

**The wow factor is a product behaviour.** The person's object becomes a scan, the scan gains meaningful markers,
and a selected marker connects to evidence. Lea's mark responds to her actual listening and speech. Give these
moments visible movement and tactile response while the interface remains usable. Follow
[Apple's motion guidance](https://developer.apple.com/design/human-interface-guidelines/motion); timings below are
LEA's design choices, not measured usability results.

### Choreography

| Moment and trigger | Normal motion | Stop or completion | Reduce Motion |
|---|---|---|---|
| Press a control | Immediate press feedback, `Motion.press` (0.22 s spring); scale no lower than 0.97 on primary actions | Release, cancel or leave control | Opacity feedback only; no scale |
| Capture lock confirmed | Brackets settle once with `Motion.settle` (0.4 s) and `Haptic.lock` | Return to stable locked outline | Immediate outline and label; same meaningful haptic |
| Real coverage accepted | Fill only the cells actually covered; progress arc follows measured state | Settle after each update; semantic updates at most 10 Hz | Immediate fill/number update; no draw-on |
| Lea listens or speaks | Mark ticks follow actual voice levels, at most 10 Hz | Stop when that audio state ends or the view disappears | Static state glyph plus caption |
| Lea is thinking | Three ticks walk while a real request is in progress | Stop on response, error, cancellation or backgrounding | Static thinking glyph and text |
| A report becomes available | Object reveal with `Motion.reveal` (0.45 s), markers with `Motion.pop` (0.35 s); first three may stagger 60 ms | One reveal per new report; never replay on routine refresh | Final object and marker positions immediately; optional 120 ms opacity change |
| Report sheet arrives | `Motion.rise` (0.4 s), no overshoot; text can be read and controls used immediately | Settle once, retain position on updates | Final layout immediately |
| Choose a finding or walkthrough step | `Motion.glide` (0.95 s) links the actual object region, marker and evidence | Interrupt on touch or another selection; settle on the chosen region | Immediate selection and framing; spoken/text label explains the relationship |
| Change ordinary status | Update wording immediately; optional 120 ms ease-out opacity transition | One transition for that change | Immediate final state |
| Safety appears or changes | Immediate, persistent content, with no entrance delay | Changes only when the actual safety content changes | Identical immediate content |

**One focal movement at a time.** A report can have a coordinated object/marker/sheet arrival, but competing
background motion must stop. Do not stagger reading paragraphs, safety notes or actionable controls. Do not make
someone wait for a flourish before they can use the result.

**The object can move because it is the object.** A deliberate, bounded reveal or a user-controlled rotation of the
actual 3D scan is appropriate. An automatic reveal stops on the first interaction; its terminal view is stable.
Do not add perpetual idle spinning, fake scan geometry or decorative floating shapes. Walkthrough movement follows
the selected real finding and remains interruptible.

**Glass stays still.** Depth comes from the frosted fill, edge and highlight in §3. No roaming shine, animated blur,
cursor-following spotlight or ambient glow loop. Camera evidence is never warped to produce a material effect.

**Every moving part has a contract.** Document trigger, animated property, token/timing, final state, interruption,
Reduce Motion and Reduce Transparency behavior. Reuse `Motion` and `Haptic`; no arbitrary animation values inside
individual screens. Use one haptic at the meaningful state change, not on every animation frame.

**Settings changes take effect while the screen is open.** Enabling Reduce Motion cancels optional movement and
settles at the current meaningful state. Backgrounding or dismissal cancels loops and pending choreography.
Static captions and state labels remain available throughout. VoiceOver announcements describe results once and
do not wait for an entrance animation.

### Motion acceptance

- Review start, middle and settled frames, an interruption, repeated updates and leaving/re-entering the screen.
- Confirm text, safety and controls are usable before, during and after the transition.
- Check Reduce Motion and Reduce Transparency both before launch and while motion is active.
- Inspect frame pacing on a real iPhone while scanning, uploading and speaking; do not claim a performance target
  passed from a Figma timeline or static image.
- Record native results in [the A7 protocol](UXHarness/native-validation.md). These indications are specified;
  runtime animation and device accessibility have not been signed off.

## 7. Screen by screen

What the Figma prototypes show. Each screen in light and dark; capture over a camera stand-in.

### 7.1 Home and scans list (No. 10, 24, 28)

- Masthead: the `lea` wordmark with her violet point, and Settings as a round outline icon button. No tagline, no
  rule under it.
- First run: "Find what's broken", the line from `main` ("Walk around it with your phone. Lea shows you what looks
  wrong, and where.") and the four steps from `main` as a numbered list (lock on, walk around it, a few close-ups, read
  the report). The numbers are real: it is a sequence.
- Each past scan is one row: crop thumbnail (48 pt, radius 6), the person's own words as the title in SF Pro
  semibold ("Scan" when they gave none, as `ScansView` does), date and photo count in `secondary`, and a verdict tag
  in `main`'s words ("Likely problems found", "No problems seen", "Not enough to call it yet"). A scan with a safety
  problem among its most likely problems shows "Safety problem" in oxblood instead (a change for the Theme PR). Rows
  are separated by hairlines, not boxed one by one.
- Bottom: the talk bar. "What happened? (optional)" with the dictation mic in `secondary`; then "Talk to Lea" (a quiet
  capsule with her mark and her name, which opens the conversation, 7.7) beside Start scan in violet. Start scan is
  the wider of the two.
- Not paired yet: no Talk to Lea (she lives on the server, so `main` shows her only once paired), Start scan full
  width, and "Not connected · Set up" in ochre under it, as on `main`.

### 7.2 Consent, pairing, settings (No. 08, 29, 30)

Alegreya Sans screen title, grouped rows on `surface`. At most one drawing per screen.

- **Consent** says what `main` does, row for row: first, to the lea server; then analysed by Claude and Jev
  (Anthropic and TypeSafe); location and photo details removed; only what you scan. "Agree and continue" in
  violet; "Not now" is a text button, not a second capsule.
- **Pairing**: "Connect to the lea server", the server address and the pairing code (masked as you type: a `SecureField` on `main`), Pair in
  violet, Skip in the navigation row. The developer note is labelled "For developers".

### 7.3 Capture (No. 12-17)

- Every line is chalk over a keyline. No paper colour anywhere on this screen.
- Lock brackets: open corners with square ends and a mitred corner, no rounding (No. 12). Searching: chalk 1.5 pt.
  Locked: violet 2.5 pt, settles once with `Haptic.lock`. Both over a keyline. The RealityKit box draws the same
  corners.
- Dome dial: an astrolabe (No. 13). Smoke disc, a hairline rim with 12 ticks and a small mark at sector 0. Empty cells
  are 1 pt chalk outlines at 55 %, covered cells chalk at 85 %, the next cell violet with an index tick on the rim
  beside it, all covered cells leaf when done. "Here" is a compass needle on the rim (No. 14).
- Cue banner: smoke capsule, chalk SF Pro text, a tone disc with a dark glyph. The cue is what Lea says aloud.
- Counter pills: smoke capsule, chalk numerals ("14 photos").
- One big button: chalk capsule, ink label.

### 7.4 Report progress (No. 02, 13, 29)

The orb is a construction drawing: a hairline circle with an inscribed square (No. 29), the progress arc drawn on in
ink over it. Failed turns the arc and glyph oxblood. Steps read like a notebook index: numbers in a fixed 20 pt
column, SF Pro titles, done steps in `secondary` with an ink check, the active step in ink with a small spinner. On `main` the waiting state is the scan turning on the dark
stage; the Theme PR keeps that and draws the stage's ground as below (7.6).

### 7.5 Report (No. 04-06, 09, 18-23, 25)

The object leads: the report opens on the stage, as on `main`, and the sheet rises over it.

- **Stage** (400 pt, `stage`, the same in both modes): Back and the voice toggle in chalk, the scan in true colour
  filling the stage on a construction-circle ground, and a `Marker` on each of the most likely problems, where it was
  found (as `StageMarks.pins` on `main`; "Also worth a look" gets no marker). Tapping a marker scrolls to its card.
- **Sheet** (canvas, 16 pt top corners, overlapping the stage by 16 pt), sections 32 pt apart, in this order:

1. **Verdict.** The top finding in `heroTitle`, in the backend's words ("Power cable: jacket split near the plug",
   as `ReportWords.headline` builds it). Then one sentence with the counts `main` puts in its eyebrow ("Two likely
   problems, one of them a safety problem. Lea couldn't check three spots.") and one meta line: your own words in
   quotes, the photo count, the date. No eyebrow above it and no rule under it.
2. **Safety card.** Opaque `surface`, one warning symbol and a direct heading (the current specimen says
   "Disconnect power"). Two short instructions preserve the disconnect action and residual-charge hazard. In the
   native app, preserve every applicable backend safety note; do not replace real hazards with prototype copy.
   No entrance animation, auto-dismissal or success confirmation.
3. **Most likely problems.** The count is the number of cards shown. Problem cards are specimen labels: the
   `Marker` and the read-only severity label; part name in `cardTitle`; issue in the tone colour; crop on the right
   (radius 6, hairline, true colour); ruled rows for where and evidence; confidence as a plain word at the right.
4. **The next step.** The step itself is the section title ("Take a closer photo"), then the instruction in SF Pro
   and the reason in `secondary`. No "Next step" above it (that is an eyebrow, §12) and no button: its action is in
   the pinned bar.
5. **What you suspected.** Your words in quotes, then a ruled row: "What Lea found" with its badge (photos agree,
   not seen in the photos, could not check) and the backend's note. Your guess only steered where she looked.
6. **In plain words.** The report's summary, unboxed, as on `main`. Not beside Lea's mark: she doesn't say it.
7. **Fix it.** The part in `cardTitle`, named as in the problem card, then one grouped list: offers (title,
   seller, price as the backend gives it) and the replacement guide. Every word comes from the backend.
8. **Also worth a look.** `main`'s name for the other issues and cosmetic findings: one grouped row each, part and
   issue.
9. **What Lea couldn't check.** `main`'s summary ("One spot wasn't clear enough and two weren't looked at. They aren't
   counted as fine."), then one grouped list of the dashed rows with `main`'s short reasons ("Too dark in photo 4",
   "Not looked at yet") and "Show all".
10. **What photos can't tell.** The backend's limits as one footnote in `secondary` with an info icon, as on `main`.
11. **Full report with photos.** A grouped link row.

**The pinned bar** sits at the bottom in a frosted glass toolbar with the shared edge, and holds the report's one next thing:
"Ask Lea" (her mark in a quiet capsule; hold to ask about this scan, as on `main`, and it reads "Listening… let go to
ask" while held) beside Continue scanning in violet, aimed at the spot in the next step. It is the same pair as
Home's Talk to Lea and Start scan. When a report has no next step, the bar holds Ask Lea alone.

### 7.6 Problem detail and 3D model (No. 02, 21, 22, 26)

- A sheet: the grabber, then a 44 pt row with previous and next problem, where you are ("1 of 2", counting the most
  likely problems), and Done (violet).
- Header in museum-label order: part (`screenTitle`), issue in its tone, the read-only severity label, then where,
  confidence ("Medium") and likelihood ("Likely") as words. Nothing sits above the part name.
- The photo crop carries a 1 pt leader line from the defect box to its `Marker` (No. 22). The box itself is a
  1.5 pt tone-coloured corner set, the same shape as the lock brackets. The photo's caption is chalk on a smoke pill,
  as over the camera, because it sits on real evidence.
- 3D model: the scan in its true colour (principle 5) on the `stage`: a ground grid and a construction circle in
  chalk at 35 %, in both modes (No. 02, 26). Each problem is a `Marker` on the scan where it was found. The dark
  stage keeps its layout on flat deep violet, with a construction-circle ground and lavender for selection.

### 7.7 Talk to Lea (new)

The conversation before a scan (`HomeConversationSheet` on `main`). Title "Lea" and Done. One line: "Tell Lea what it
is and what's wrong. She'll work out the exact product before you scan." The product brief as a card: the product as
its title (no "This looks like" above it), then ruled rows for the known issue ("Area: why", as `ProductBrief` reads
it) and the sources by name. Before the label photo is sent, a quiet "Show me the label" button; after, a quiet line
("Label photo sent"), because a status is never a button. Start scan in violet. Then the captions, both sides in
ink, the names telling them apart (Lea's in violet). The frame shows one moment: once the brief is on screen,
Lea's latest words are the brief as `ProductBrief.spoken` says it, never "Let me look that up". When you mention heat,
smoke or a smell, Lea's first words are the safety step ("If it gets hot, unplug it before you pick it up."); this
needs a rule in her voice persona and is not on `main` yet (§10). At the bottom, Lea's mark at 88 pt with her state in
words ("Listening…", `.headline`) and one line that says what happens next ("Lea answers when you pause.": a tap on
`main` turns on hands-free, it does not end a turn).

## 8. Accessibility

- Text contrast ≥ 4.5 : 1 for every reading colour in both modes (tables above); `faint` is never used for text
  people need.
- Dynamic Type everywhere off the camera; camera text caps at `.accessibility1`, as now.
- Status always has a word; safety has a warning symbol and unknown is also dashed; over the camera, states also differ in shape
  (rim tick, stroke weight). Works with Differentiate Without Color.
- Alegreya Sans ships 100-900 upright and italic, so Bold Text has real weights to move to.
- Lea's words are captions as well as speech; VoiceOver reads the same words she says.
- Reduce Motion: use the immediate final layout or the optional short opacity change in §6; no travel, scale,
  draw-on or rotating ticks. Reduce Transparency uses opaque surfaces without backdrop blur.

## 9. Native implementation handoff

The design branch contains documentation, Figma exports, token values and the executable Figma audit. Native
SwiftUI changes are separate work; §6 is the motion specification, not a statement that the animations shipped.

- Apply §3 to `Palette`, including `onAccent`, `secondary`, `faint`, glass fill/edge and the opaque transparency
  fallback. Replace green/brass mappings with the violet semantic tokens; preserve dynamic light/dark resolution.
- Apply the shared glass treatment only to the surfaces named in §3. Keep reading and safety panels opaque;
  map Reduce Transparency to the solid surface and remove background blur.
- Use `onAccent` for primary labels: the dark lavender action needs a dark label. Do not keep a hardcoded white
  label on the new dark-mode accent.
- Move camera hues into semantic camera tokens, with `next` at `#C2B1FF` and shape cues. Do not blur evidence.
- Register and scale the §4 fonts after the §10 licence question is resolved. Keep body text on native Dynamic Type.
- Implement the §5 status labels and safety pattern; retain actual backend safety content and honest unknowns.
- Apply the §6 motion contracts to press styles, lock, coverage, Lea's mark, report arrival and selected evidence.
  Stop loops on cancellation/backgrounding and handle accessibility setting changes live. Safety remains immediate.
- Update gallery previews with light/dark, accessibility text sizes, Reduce Motion and Reduce Transparency cases.
- Run the Figma audit for design drift and the separate native/user checks in §8 and the A7 protocol. Never mark
  a runtime behavior passed from a static export.

## 10. Open questions

| Question | Owner |
|---|---|
| Add SIL OFL 1.1 (fonts only) to PLAN.md L2, for Alegreya Sans? | Owner |
| Who draws the three or four line illustrations? | Design |
| Who gets edit access to the Figma file | Design |
| The report stage from PR #13 against 7.5 and 7.6: one pass in the Theme PR | App |
| Failure screens: pairing failed, upload failed, report failed, Lea unreachable | Design |
| Capture: an empty dial before lock-on, and Lea's mark over the camera while she talks | Design |
| Before a scan, Lea leads with "unplug it" when someone mentions heat, smoke or a smell (7.7). Her persona in `backend/repairos/voice/conversation.py` only leads with safety once a report has a safety note: add one rule | Backend |
| ~~Website: same codex direction, or its own?~~ The same direction: §11 | Decided 2026-10-24 |

## 11. Website

The site uses this direction, not one of its own (§10). It lives in `website/` (stream `website`,
`areas/website.md`).

- **Colour.** The §3 tokens, light (Lavender) and dark (Violet night) by `prefers-color-scheme`. The same rules apply:
  red only for safety, violet for Lea and primary actions, no colour outside the token
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
  - **Primary action:** the violet capsule, at least 52 px tall, one per view. Everything else is an underlined
    text link.
  - **Lea:** her mark beside anything she says.
  - **Report examples:** the specimen label (§7.5) and the read-only status label (§5).
  - **Unknown:** a dashed box. Dashes mean unknown and nothing else.
  - **Features:** a ledger of ruled rows, not cards. The limits are set as notes at the foot of a page.
- **Motion.** Use the same product-linked principle as §6: a bounded reveal of an actual product figure, clear
  button feedback and evidence selection that preserves spatial continuity. Do not animate every section on
  scroll or add ambient floating objects. Use native/CSS transitions where sufficient; an interactive figure may
  use JavaScript only when it adds a real product interaction. Reduced Motion removes travel, scaling and smooth
  scrolling; all content remains readable without animation.
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
- Small uppercase tracked labels ("REPORT · 24 OCT 2026"); an eyebrow over a heading, in caps or in sentence case
  ("Next step" above "Take a closer photo"); a short accent rule under a heading.
- A status drawn as a button ("Label sent" as a capsule); the same capsule doing several jobs.
- A box around every section; a heading voice per card. Two heading levels, and boxes only where §5 allows them.
- Custom navigation rows with targets under 44 pt.
- "No. 01" catalogue numbering, or leading zeros, where the numbers are not a sequence the reader uses.
- Italic or script type for asides, confidence or anything the model judged.
- Chat bubbles for Lea; she speaks in captions.
- A grid of boxed cards where ruled rows would do; an icon in a circle on every card; emoji or Unicode glyphs
  standing in for icons.
- Arbitrary shadows, glowing edges and invented elevation levels. The shared glass material in §3 is the
  approved exception; keep reading surfaces quiet.
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

**Measure colour before committing.** Every token goes through contrast (text ≥ 4.5 : 1 on each intended surface).
Record the actual tested pairs and results. Check meaning under
colour-vision differences without importing a previous palette's ΔE measurements as evidence for a new one.

**The deletion loop.** On every first draft, remove the three most obvious generated-looking choices, then check
the layout still holds on type, rules and whitespace alone. If it doesn't, the structure is wrong; adding
decoration won't fix it. Record what was removed in §13.

**Moving parts earn attention.** Apply principle 10 and §6 before adding animation. Name the product relationship
it reveals and show its settled and accessible states. A static design can be elegant; a working interaction
should feel responsive and alive. Never invent progress to keep something moving.

**Two loops, then tighten.** At most two design iterations, then the work moves to code: contrast, spacing and copy.

**Copy is product truth.** Every claim on a screen matches `main`: who gets the photos, what each control does,
what the backend sends. A prototype that is prettier but untrue is worse than the old one (review 4, consent).

**Critique, then polish.** Before a design goes to review, run impeccable's critique with its two assessments kept
apart (a design review, and a deterministic pass: the detector where it can read the files, a lint of the Figma file
where it cannot), then a polish pass on every P0 and P1. Record the score in §13.

**Checks.**

- Every text and background pair passes 4.5 : 1, in both modes (§3, §8). The proprietary guard passes.
- **Figma:** no raw colours outside camera and evidence stand-ins; spacing on the scale; every text on a style;
  targets ≥ 44 pt; light and dark frames identical in structure.
- **iOS:** Xcode Accessibility Inspector on every screen, and SwiftLint.
- **Web:** a contrast check, and a look at 390, 768 and 1440 px in both modes.

**iOS: Apple's Human Interface Guidelines, within the codex.**

- **Containers.** Build with native SwiftUI containers (`NavigationStack`, `List`, `ScrollView`, `Grid`, `Form`)
  and system controls. Don't wrap a web-style card template around them.
- **Type and icons.** Body text uses SF Pro at Dynamic Type text styles (`.body`, `.headline`, …). Alegreya Sans
  scales with `relativeTo:`. Icons are SF Symbols, using the outline variants (§5).
- **Touch and gestures.** Tap targets are at least 44 × 44 pt, content stays inside the safe area, and there are
  no gestures the system doesn't already use.
- **Colour.** Theme.swift uses semantic tokens for the Lavender and Violet night appearances. Adapt to light, dark and
  Increase Contrast, and verify the actual result. Screens never use a raw colour.
- **Materials.** Use native materials for system navigation, sheets and menus. The shared frosted treatment in §3
  is allowed on the named custom surfaces, with an opaque Reduce Transparency fallback. No decorative blur over
  the camera or evidence (§2.6).
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

Review 4, 2026-10-25, an impeccable critique of every screen and component (design review and Figma lint run
apart), then a polish pass. Score 27/40 before the polish (Acceptable).

1. P0: Consent and Pairing contradicted `main` ("nothing goes to a cloud", "only to your Mac"). Both now say what the
   app does: the lea server, then Claude and Jev; "Agree and continue"; the server address; Skip.
2. P1: the report had no next action and opened on text. It now opens on the stage with markers, Next step carries
   the verdigris Continue scanning, and Ask Lea is pinned.
3. P1: six heading voices and surviving sentence-case eyebrows. Two heading levels; Next step, What you suspected,
   Fix it and What Lea couldn't check became sections; boxes only for safety, problem labels and grouped lists.
4. P1: Lea's entry was an unlabelled glyph; on 09 a status was a button and the state was 13 pt. "Talk to Lea" is
   labelled; "Label photo sent" is a line; "Listening…" is a headline; Lea leads with "unplug it" when it gets hot.
5. P2: numbers and counts disagreed. One `Marker` everywhere; counts match the cards; the Scans list escalates a
   safety scan to "Safety problem".
6. Lint fixes: the photo caption (2.5 : 1) moved to chalk on smoke; 23 off-scale spacings onto the scale; 44 pt
   navigation with Back, Done and Skip in the tint; the 08 nav's empty 100 pt frame removed; 06's home indicator
   added; step numbers in a fixed column; icon meanings untangled (trash, location, upload, possible problems);
   the gear redrawn; draft-2 backdrops on the Components page made neutral.
7. Not done: failure screens (pairing, upload, report), the empty dial before lock-on on 04, and Lea's mark over the
   camera. They are §10 follow-ups.

Review 5, 2026-10-25, the same five priorities again, with every word on screen read against `main`'s code rather
than against the critique's wording. Review 4 had fixed the headline of each one; this pass fixed what was left.

1. P0, copy that `main` doesn't say. Scan rows were titled with object names ("Kettle base"); `main` titles them
   with your own words, or "Scan". Verdict tags said "Possible problems" and "No visible problems"; `main` says
   "Likely problems found" and "No problems seen". Home showed Talk to Lea while not connected; `main` hides it until
   paired. The pairing code showed as digits; it is a masked field. The safety card, the limits and the couldn't-check
   rows were our own sentences; they are now the backend's. "Lea said" showed the written summary, which Lea never
   says; it is "In plain words" again. On 09, Lea was "looking it up" while the brief was already on screen, and the
   help line said a tap ends your turn; it turns on hands-free.
2. P1, the report's one next thing. Continue scanning sat at y ≈ 1,860 inside the scroll; it is now in the pinned
   bar beside Ask Lea, the same pair as on Home. The headline is the top finding, as on `main`, and the scan fills
   the stage with markers on the two likely problems only.
3. P1, heading voices and eyebrows. "Next step" above "Take a closer photo" was the example §12 bans: the step is the
   title now. "This looks like" above the product and "Problem 1 of 3" above the part are gone (the position is in
   08's navigation row). "What photos can't tell" lost its third heading voice and is a footnote. Fix it shows one
   offer, not two identical ones. The report is 2,955 pt, down from 3,143.
4. P1, talk first. Covered by 2 and 3: Lea is the left half of the report's bar, and 09 shows one coherent moment.
   The unplug line stays, flagged in §10 as a backend rule.
5. P2, numbers. "Seen twice" against "only one photo", "Power cable" against "Power cord", "Problem 1 of 3" against
   two likely problems, and a marker on a cosmetic finding `main` doesn't mark: all made to agree.


Review 6, finalized violet/glass direction and motion handoff:

1. User selected a purple palette related to the cosmetic tone. Green/brass and warm neutrals were replaced by
   violet actions, lavender light surfaces and deep-purple dark surfaces; §3 records actual resolved Figma values.
2. Shared frosted glass was applied to controls, cards and floating bars. Flat-only and no-glass prohibitions were
   removed from the active rules; opaque reading/safety surfaces and the transparency fallback are explicit.
3. The safety specimen became one warning symbol and two concise instructions. Status capsules and decorative
   sparkles were removed; safety and unknown states retain distinct treatment.
4. Principle 10 and §6 make animation and moving parts part of the wow factor: the actual scan, lock, coverage,
   Lea's voice and evidence selection. Each has a trigger, timing, terminal state and accessible alternative.
5. The executable Figma audit passes 1,153 assertions. The 30 tested semantic text pairs have a 4.79:1 minimum.
   Native Dynamic Type, VoiceOver, frame pacing and first-run user validation remain NOT RUN.
6. All exported screen snapshots, cover, foundations and components were refreshed from the live file. Exports
   are review images, not proof that the native app or motion specification has shipped.
