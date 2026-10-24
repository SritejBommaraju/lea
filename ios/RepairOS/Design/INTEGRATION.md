# Capture overlay integration

For the capture screen (`Capture/**`, `ObjectCapture/**`). The pieces live in `Design/CaptureOverlays.swift` and
`Design/Components.swift`; tokens in `Design/Theme.swift`. Previews: "Capture overlay", "Cue tones", "Dome dial".

## Layout

```
ZStack {
  camera (ARView / ObjectCaptureView)
  LockBrackets(phase:)            // over the object's projected rect, or the 3D box in RealityKit (LockBoxSpec)
  ShotTick(trigger: shotCount)    // centred
  VStack {
    HStack(alignment: .top) { close button · Spacer · DomeDial }
    Spacer()
    CueBanner(text:symbol:tone:)
    StatusPill / ProgressPill row (optional: photos, upload)
    Button { … } label: { Label("Close-ups", systemImage: Symbol.closeup) }.buttonStyle(.primary(.light))
  }
  .padding(.horizontal, Spacing.l)
}
```

Close button: `Button { … } label: { Image(systemName: Symbol.close) }.buttonStyle(RoundIconButtonStyle(onCamera: true))`.

## Wiring

| Component | Feed it | Replace |
|---|---|---|
| `DomeDial(cells:here:done:)` | `(0..<CoverageDome.cellCount).map { dome.isCovered($0) ? .covered : ($0 == nextCell ? .next : .missing) }`; `here` = the phone's current cell `% 12`; `done: dome.isDone`. Index layout is the same as `CoverageDome` (ring × 12 + sector). | `CoverageDial` in `CaptureView.swift` |
| `CueBanner(text:symbol:tone:)` | The current cue. Tone: `.guide` for directions ("Move left", "Other side"), `.caution` for fixable (slow down, more light, too far), `.stop` for blocking (too close, object lost), `.good` when the dome is done. | the `cueText` capsule |
| `ShotTick(trigger:)` | `shotCount` (Int, +1 per accepted photo). Fires `Haptic.shot` itself. | `.sensoryFeedback(.success, trigger: shotCount)` |
| `LockBrackets(phase:)` | `.searching` until the box is fitted, then `.locked` (settles in once and fires `Haptic.lock`). Set its `.frame`/`.position` from the projected box at ≤ 10 Hz. | `TargetRing` / the `+` crosshair |
| `.buttonStyle(.primary(.light))` | The one action per stage (Lock on / Close-ups / Finish). | `bigButton` |
| `StatusPill(…, onCamera: true)`, `ProgressPill(…, onCamera: true)` | Photo count, upload progress. | — |

Suggested symbols: walk → `figure.walk`, slow down → `tortoise.fill`, more light → `lightbulb.fill`, too close →
`arrow.down.backward.and.arrow.up.forward`, too far → `arrow.up.forward.and.arrow.down.backward`, other side →
`arrow.triangle.2.circlepath`, higher/lower → `arrow.up` / `arrow.down`, left/right → `arrow.left` / `arrow.right`,
lock → `Symbol.lock`.

Haptics: keep `Haptic.milestone` for a completed ring or the full dome and `Haptic.warn` when the cue turns
`.stop`; drop the per-tick `.impact(weight: .light)`. One buzz per meaningful moment.

## RealityKit lock box

Draw only the 12 corner segments of the box (each arm `LockBoxSpec.cornerFraction` of the shorter edge), as thin
boxes with `UnlitMaterial(color: LockBoxSpec.uiColor(phase))`, 1.5 mm thick searching, 2.5 mm locked. No lighting,
no shadows. On lock, scale the entity from `LockBoxSpec.lockOvershoot` to 1 over ~0.4 s (skip under Reduce Motion)
and fire `Haptic.lock` from SwiftUI on the same state change.

## Performance rules

- Drive every overlay from coarse published state, ≤ 10 changes a second. Never pass per-frame values (camera
  transform, raw azimuth) into these views; quantise first (cell index, cue enum).
- No `.ultraThinMaterial` except at most one bar; these components use solid translucent fills instead. No
  shadows or animated gradients over AR.
- `DomeDial` is flattened with `.drawingGroup()`; keep it a fixed size.
- Reduce Motion is handled inside each component.

## Other app changes that touch the capture side

- **Consent:** `RootView` shows `ConsentView` once before the first scan. `--autostart-scan` skips it, so USB
  smoke tests still start unattended.
- **Report:** `ReportView(record:upload:close:)` keeps its signature. It now caches the report JSON at
  `<session>/report.json` (the scans list reads the verdict from it) and shows the scan in 3D from
  `scans/<id>/*.ply` (preferred: same frame as `location.point_m`) or `scans/<id>/*.usdz|obj`.
- **Deleting scans:** `App/ScansView.swift` → `ScanLibrary` uses `ScanStorage.canDelete(_:)`, `delete(_:)`,
  `deleteAll()` and `usedBytes()`; the session being captured is never offered for deletion. Only the phone copy
  is removed ("Also delete from my Mac" is a disabled placeholder in Settings).
