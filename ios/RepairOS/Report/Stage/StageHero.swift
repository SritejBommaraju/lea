import SwiftUI

/// What the dark stage shows. `.automatic` picks the 3D view or the photo hero from the report's model source;
/// `.custom` hosts any other hero (the photo-real recreation, Report/Recreation) with the same markers and focus:
///
///     ReportStageView(..., hero: .custom { ctx in AnyView(RecreationView(source: src, markers: ctx.markers, focus: ctx.focus,
///                                                                      onMarkerTap: ctx.select, onLoaded: ctx.ready, probe: ctx.probe)) })
enum StageHero {
    case automatic
    case custom((StageHeroContext) -> AnyView)
}

/// Everything a hero needs from the report: the numbered markers (plus circled "look closer" rings, which carry a
/// `ringRadius`), the focused one, and what to call when one is tapped.
struct StageHeroContext {
    let markers: [ModelPin]
    let focus: String?
    let select: (String) -> Void
    /// Call once the hero is drawn: the landing (markers, verdict) waits for it, at most 2.5 s.
    let ready: () -> Void
    /// Pass to the hero so circle to inspect can find the spot on it.
    let probe: StageProbe
}

/// After a circle on the stage: look closer now (Continue scanning aims at it), keep it marked, or drop it.
struct LookCloserSheet: View {
    let canScan: Bool
    let scan: () -> Void
    let keep: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Label("Look closer here", systemImage: "plus.magnifyingglass").font(.headline)
            Text(canScan ? "Lea will guide you back to this spot with the camera and take sharp close-ups of it."
                         : "Lea keeps the spot marked on the scan. Scan again to look at it closely.")
                .font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if canScan {
                Button(action: scan) { Label("Look closer", systemImage: Symbol.scan) }.buttonStyle(.primary)
            }
            HStack {
                Button("Keep it marked", action: keep).font(.subheadline.weight(.semibold))
                Spacer()
                Button("Cancel", action: cancel).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            }
            .frame(minHeight: 44)
        }
        .padding(Spacing.gutter)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}
