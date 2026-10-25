# Violet and frosted glass

User-directed Figma revision, 2026-10-24. This supersedes the earlier green/brass palette and flat-only material direction. Production SwiftUI has not been changed.

[Review light and dark screens](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u?node-id=95-1464). [Component regression board](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u?node-id=86-157).

| Semantic role | Light | Dark |
|---|---|---|
| Canvas | `#F3F1FA` | `#12101B` |
| Reading surface | `#FBFAFF` | `#211D30` |
| Well | `#E9E4F5` | `#2C263D` |
| Primary text | `#211C32` | `#F5F1FF` |
| Secondary text | `#625B73` | `#BEB5D0` |
| Action | `#6650CC` | `#C2B1FF` |
| Cosmetic | `#75638C` | `#C2B3DB` |
| Glass fill | White at 76% | `#302844` at 82% |
| Glass edge | White at 92% | `#DDD1FF` at 24% |

The action is a saturated violet in light mode and lavender in dark mode, related to the former cosmetic purple. Cosmetic status uses a quieter plum and an explicit label. Safety retains its danger tone and warning symbol; unknown remains explicitly dashed. Photos and scan evidence retain their original colours.

The shared `Material/Frosted glass` effect style uses 24 px background blur, a restrained white inner highlight and a soft violet shadow. Glass is applied to quiet controls, app icon buttons, problem cards, brief cards, bottom bars, pinned toolbars and selected grouped panels. Long reading panels and safety notices retain opaque surfaces. Glass sits over the known app canvas; this revision does not promise text contrast over arbitrary camera imagery.

For native Reduce Transparency, substitute the opaque reading surface and remove background blur. Text and symbols retain full opacity. Keep material static; apply the existing immediate-safety and Reduce Motion indications.

Existing semantic variable IDs and component links were preserved. New glass tokens alias primitives; raw export values and IDs are in `palette.json`. Cover, foundation notes and screen labels now reflect the violet direction.

Validation: 1,153 executable Figma assertions pass. The audit checks six actual-component layouts, glass-style linkage, reflow, and 30 semantic text pairs. The lowest tested ratio is 4.79:1 (cosmetic on the light canvas); fixture text alone is at least 5.75:1. Light/dark screen composition and large-text specimens were visually reviewed. Device accessibility and uncoached-user checks remain NOT RUN; see `native-validation.md`.
