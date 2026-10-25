# Figma component UX harness

Runs assertions against the actual LEA Figma components and linked regression instances. No backend, camera, scan, or repair result is simulated.

File: `acZpRbLDyuavWtoJIqyj1u`. Regression board: [Warnings and findings](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u?node-id=86-157).

## Run

Load the `figma-use` skill, create a Figma MCP session for the file, then pass the complete contents of `audit.js` as the `code` argument to `use_figma`, with that file key and session ID. This is Figma Plugin API JavaScript, not a Node.js program. The response is JSON; `passed` must be true. Save the result to `latest-results.json` after any component change. The audit is read-only apart from selecting the Components page.

The board contains six cases: 288 pt and 353 pt content widths at normal size, and 288 pt at 200% text, each in light and dark. They remain linked to the production design components, so source changes are tested on rerun. IDs are intentional stable fixture references; a missing board fails rather than silently recreating it.

Checks cover component linkage, text containment, product font, text contrast, misleading underline styling, non-interactive status labels, distinct uninspected state, and editable vector/text content. The 4.5:1 threshold follows [WCAG text contrast](https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum).

## Revised design

Safety uses a neutral surface, one warning symbol, “Disconnect power,” and two concise instructions. It retains the residual-charge warning and limits battery removal to user-removable batteries. Status labels use quiet text, with safety emphasized and uninspected regions explicitly dashed. Evidence counts describe photos, not certainty. These are Figma design changes; production SwiftUI is unchanged.

Motion indications follow [Apple motion guidance](https://developer.apple.com/design/human-interface-guidelines/motion): safety appears immediately and persists; normal status changes may use a 120 ms ease-out opacity transition; Reduce Motion uses an immediate final state. No decorative loops, travel, scaling, or staggered safety text. These are implementation indications, not a claim of runtime animation coverage.

## Required device checks

- VoiceOver reads warning heading, instruction and residual hazard in order; a changed result is announced once.
- Native Dynamic Type and Bold Text wrap without hiding content.
- Interactive controls around these read-only labels have at least 44 × 44 pt targets.
- Reduce Motion suppresses optional transitions; safety is never delayed or auto-dismissed.
- A new user distinguishes an inspected area with no visible issues from an uninspected area, and can explain the required safety action.

Do not mark A7 complete from this automated audit: its acceptance criterion requires real people completing scan-to-report tasks.
