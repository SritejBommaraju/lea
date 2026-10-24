# LEA design: Figma snapshots

Exports of the [Figma file](https://www.figma.com/design/acZpRbLDyuavWtoJIqyj1u) on 2026-10-24, for review of
[DESIGN.md](../RepairOS/Design/DESIGN.md) (draft 2). The Figma file is the source of truth; these go stale when it
changes. Inter stands in for SF Pro, JetBrains Mono for SF Mono and drawn icons for SF Symbols. Photo and 3D slots are
empty on purpose: real evidence keeps its true colour.

They live here, outside `ios/RepairOS/`, because Xcode syncs that folder into the app target and would bundle them.

## Cover

![Cover](00-cover.png)

## Screens

Paper (light) and walnut (dark). 04 and 05 are the capture screens: the camera never turns into paper, so they are
the same in both modes.

| | 01 Home, first run | 01b Home, scans | 02 Consent | 03 Pairing |
|---|---|---|---|---|
| Light | <img src="01-home-first-run-light.png" width="200"> | <img src="01b-home-scans-light.png" width="200"> | <img src="02-consent-light.png" width="200"> | <img src="03-pairing-light.png" width="200"> |
| Dark | <img src="01-home-first-run-dark.png" width="200"> | <img src="01b-home-scans-dark.png" width="200"> | <img src="02-consent-dark.png" width="200"> | <img src="03-pairing-dark.png" width="200"> |

| | 04 Capture, searching | 05 Capture, locked | 06 Report progress | 08 Problem detail |
|---|---|---|---|---|
| Light | <img src="04-capture-searching.png" width="200"> | <img src="05-capture-locked.png" width="200"> | <img src="06-report-progress-light.png" width="200"> | <img src="08-problem-detail-light.png" width="200"> |
| Dark | (same) | (same) | <img src="06-report-progress-dark.png" width="200"> | <img src="08-problem-detail-dark.png" width="200"> |

### 07 Report, full scroll

| Light | Dark |
|---|---|
| <img src="07-report-light.png" width="300"> | <img src="07-report-dark.png" width="300"> |

## Foundations

Principles, colour in both modes, camera tokens, type and line weights.

![Foundations](00-foundations.png)

## Components

![Components](00-components.png)
