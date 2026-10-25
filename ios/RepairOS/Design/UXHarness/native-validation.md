# A7 native and first-run validation

Status: **NOT RUN**. Figma checks do not satisfy this gate. The current agent host has only `/Library/Developer/CommandLineTools`; `xcrun simctl` is unavailable and Spotlight found no Xcode installation. A teammate will run the device session; results have not yet been received.

## Identify the build

Record the app commit/build, iPhone model, iOS version, tester alias, date, backend revision if known, and real scan/session ID. Verify a live gateway is paired and reachable. Keep credentials out of test records. Use real hardware and actual captured reports, not the gallery's default examples or synthetic scan. The Figma revision is not implemented in SwiftUI by the harness PR; record which UI is actually being tested.

For a teammate building from source, follow `ios/README.md` (Release device build, signing, Metal toolchain). Have them report Xcode version and build errors verbatim if it fails. Do not silently substitute a replay for an end-to-end run. A saved real session is acceptable only for repeating report accessibility checks and must be identified as a replay.

## Device matrix

Run the full route once at the default text size and once at the largest accessibility size. Check the report and its sheets at every accessibility text-size stop. Repeat the largest-size report check in light and dark appearances, with Bold Text on. In Settings → Accessibility → Display & Text Size → Larger Text, enable Larger Accessibility Sizes; see [Apple's instructions](https://support.apple.com/guide/iphone/make-text-easier-to-read-iph3c076905a/ios).

| ID | Action | Pass evidence |
|---|---|---|
| DT1 | Read consent and pairing, including keyboard-open fields and errors | Complete copy, reachable controls, no clipped or overlapping labels |
| DT2 | Start a real scan, follow cues and close-ups, finish and upload | Instructions remain legible; Start/Done/close controls stay reachable; any text-size cap is documented |
| DT3 | Read the report, safety details, findings, evidence and uninspected regions | Full text can be reached by scrolling; no truncation hides safety, uncertainty or an action |
| DT4 | Open and dismiss every relevant sheet; reopen the saved report | Dismissal remains reachable, no focus trap or lost navigation |
| VO1 | Enable VoiceOver and navigate first-run screens | Useful labels, roles and focus order; each action activates with VoiceOver |
| VO2 | Complete capture and close-ups with VoiceOver | Cues are understandable; essential controls can be found and activated without visual searching |
| VO3 | Receive a report with Lea muted, then check with Lea speaking on a separate real report | Result is announced intelligibly; no duplicate or competing speech, no unexpected focus theft |
| VO4 | Explore safety, evidence and unknown regions | Safety instructions and residual hazards are read; unknown is never announced as success; modal focus enters and returns correctly |
| RM1 | Turn Reduce Motion on before launch, then toggle during the session | Optional movement stops; safety is immediately available; state changes remain clear |
| RM2 | Press primary, icon, card and report-stage controls | No unnecessary scale movement under Reduce Motion; actions still give perceptible feedback |

Capture a screenshot for each layout failure and a timestamped observation for each spoken/focus failure. With participant consent, retain a screen recording locally and record its path rather than uploading identifiable recordings to the repo. A pass requires observation on the named build, not a code comment or a Figma screenshot.

## Uncoached first-run task

Use people who have not used LEA before; include a habitual VoiceOver user for assistive-technology usability rather than treating a sighted developer's pass as equivalent. Record participant aliases only. Have each use an object that was not used to develop the app. Use objects safe to handle without repair or disassembly; the task is scanning and interpreting the report.

Read only: “Use LEA to inspect this object. When you are finished, explain what it found, what it could not check, and what you would do next.”

Do not tell them where to tap. Observe consent/pairing, scan, close-ups, upload, report, evidence and next action. Record elapsed time, wrong turns, requests for help, recovery and the participant's explanation. If intervention is necessary, record it; the attempt is not an unassisted pass. A coached retry is practice, not a replacement first-run result.

Use naturally occurring report states. If no real capture produces an uninspected or insufficient-evidence case, mark that check not covered; do not alter the report to manufacture the state. Real saved captures can supplement the report-reading checks, explicitly labelled as such.

## Results to return

Copy this block for each run. Leave missing checks NOT RUN.

```text
App commit/build:
Device / iOS / Xcode (if built locally):
Backend revision (if known):
Tester alias / prior LEA experience / habitual VoiceOver user:
Object alias / new to development:
Live scan/session ID or identified replay:
Text size / Bold Text / appearance / VoiceOver / Reduce Motion:
Checks passed (IDs):
Checks failed (IDs, exact steps, expected vs observed):
Checks not covered:
Evidence paths and timestamps:
Unassisted scan-to-report: PASS / FAIL / NOT RUN
Interventions / wrong turns / elapsed time:
Participant explanation of findings, unknowns and next action:
Issue links and retest build:
```

## Source-review leads, not device results

- `PrimaryButtonStyle` limits labels to one line and scales them down; test longer labels at maximum type sizes.
- `PrimaryButtonStyle`, `RoundIconButtonStyle`, `PressableCardStyle` and stage control styles animate scaling without a local Reduce Motion check. Verify the runtime behavior and fix before marking RM2 passed.
- Stage capsule controls and camera status components cap text at `accessibility1`; inspect readability and whether the native accessibility-size requirement is met.
- `StageSafetyLine` changes from an expandable button to inline notes at accessibility sizes; test both reading order and completeness.

## Closure

The reviewer must have actual run evidence for native layout, VoiceOver and unassisted scan-to-report. File every observed obstacle with reproduction steps, owner and severity. Retest fixes on the identified build; any remaining safety or task-blocking defect prevents sign-off. Record any deferred issue explicitly. Do not mark A7 complete while the device or participant checks remain NOT RUN.
