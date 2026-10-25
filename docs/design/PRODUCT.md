# Product

<!-- impeccable:product-schema 1 -->

## Platform

ios

## Users

Anyone at home with something that stopped working: a kettle, a speaker, a game controller, a power tool. They are
at the kitchen table or in the garage, the object in front of them and the phone in one hand, often talking rather
than tapping. Most are not repair people. Confirmed 2026-10-24.

## Product Purpose

Lea is a repair copilot you talk to. You tell her what's wrong and what you think the cause is; she works out the
exact product (from what you say and a photo of the label) and what public sources report about it; you scan the
object with the phone's LiDAR camera while she talks you through it; then she says what looks wrong, with the photo
evidence, and what she couldn't check. Success: the person knows what is most likely broken, how sure Lea is, what
is still unknown, and what to do next (a closer photo, a meter reading, a part).

## Positioning

Lea looks at your actual object, from every side, and is honest about it. Every finding has its photo; confidence is
a word, never a percentage; parts nobody looked at are "not inspected", never "fine"; only a meter reading proves an
electrical fault. Your own guess steers where she looks but never becomes a finding.

## Operating Context

- Hands are busy: one holds the phone, the other turns the object. Voice ("Hey Lea", swipe up, hold to talk) is a
  first-class input, with captions for both sides.
- The scan is a walk around the object; the coverage dome fills as photos take themselves, then Lea asks for a few
  close-ups.
- The report lands with Lea's voice: the scan in 3D on a dark stage, numbered markers on it, and the report sheet
  rising over it.
- Light varies from a bright kitchen to a dim garage; the camera screens have no light or dark mode.

## Capabilities and Constraints

- Shipped on `main` (demo build, PR #13): pre-scan conversation with product identification and a public-sources
  brief; voice during the scan; the stage report with markers, circle to inspect, "What you suspected", "What Lea
  couldn't check" and Fix it (replacement part and guide link); Ask Lea about a report.
- The code calls the product RepairOS; people only ever see "lea".
- Invariants from PLAN.md: not looked at is `not_inspected`, never `ok`; `insufficient_evidence` always has a reason;
  confidence stays a word (I6); vision is a suspect and only a meter reading is proof; no product names in code.
- Licence-clean: permissive code, weights and fonts only (PLAN.md L2).
- No Xcode on the design machine: Figma prototypes first, then `Theme.swift`.

## Brand Commitments

- Name: "lea", lowercase in the wordmark; Lea is "she" in the app's copy and code.
- Voice: a friend who knows repair. Warm and calm, plain words, short sentences. Takes your guess seriously, says
  how sure she is in words, admits what she couldn't check, never talks down. Confirmed 2026-10-24.
- The coverage dome (rings of cells that fill as you walk around) is the app icon and the capture dial: it is the
  product's own mark.
- Final visual direction: violet actions, lavender/deep-purple neutrals and restrained frosted glass. The
  coverage dial, leader lines and evidence markers retain LEA's identity. The former green/brass and flat-only
  direction is superseded by the latest user feedback; see DESIGN.md §§2–6.
- Rejected 2026-10-24: draft 2's rendition of it (cream paper, walnut brown, EB Garamond, italic margin notes, tracked
  mono labels). It read as a generated report, not as Lea. See DESIGN.md §12 and §13, review 3.

## Evidence on Hand

- The real app on `main`: screens, copy and flows above.
- App icon: `ios/RepairOS/Assets.xcassets/AppIcon.appiconset/` (the coverage dome).
- No real captures, customers, testimonials or metrics are in the repo. Prototypes leave photo and scan slots as
  marked placeholders; nothing is invented.

## Product Principles

1. Talk first. Lea is someone you talk to while your hands are busy; the screens back up the conversation.
2. Honest by construction. Unknown never looks like fine; a guess never looks like a finding.
3. Your object is the hero. The photos and the scan keep their true colour and lead every screen they are on.
4. One next thing. Every screen ends in the one action that moves the repair on.
5. Motion creates the wow factor. The actual scan, lock, coverage, voice and evidence respond visibly to real
   activity. Each moving part has a stopping condition and an accessible alternative (DESIGN.md §6).

## Accessibility & Inclusion

Dynamic Type everywhere off the camera; VoiceOver hears the same words Lea says; status is never colour alone;
Reduce Motion settles movement immediately or uses a short opacity change; Reduce Transparency uses opaque
surfaces. Design contrast checks pass; native accessibility and real-user validation remain NOT RUN.
