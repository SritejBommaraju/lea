import Foundation
import SwiftUI

/// "Look closer here": an area the person circled on the report's 3D stage (or held a finger on). A point and a
/// radius in the scan's frame (metres, ARKit world, Y up: the same frame as `location.point_m`), kept beside the
/// session in `inspect_targets.json`. Continue scanning reads the newest unchecked one and shows its "Move closer
/// here" marker on that spot; it stays drawn on the stage until a scan has been back to it.
///
/// Kept on the phone only: the gateway's story endpoint takes text (`source` typed|voice, extra fields forbidden),
/// so there is nowhere to send a 3D target yet.
struct InspectTarget: Codable, Identifiable, Sendable, Equatable {
    var id: String = UUID().uuidString
    /// Centre, scan frame (metres).
    var point: [Float]
    /// Radius around it worth a close look (metres).
    var radius: Float
    /// The photo it was circled on, for a photo hero (no 3D point then).
    var stillID: String?
    var createdAt = Date()
    /// A continued scan has been back to it.
    var checked = false

    var simd: SIMD3<Float>? { point.count == 3 ? SIMD3(point[0], point[1], point[2]) : nil }
}

/// The session's circled targets, on disk next to the session (read and written on the main actor; tiny files).
enum InspectTargets {
    static func url(_ sessionDir: URL) -> URL { sessionDir.appending(path: "inspect_targets.json") }

    static func load(_ sessionDir: URL) -> [InspectTarget] {
        guard let data = try? Data(contentsOf: url(sessionDir)) else { return [] }
        return (try? JSONDecoder().decode([InspectTarget].self, from: data)) ?? []
    }

    static func save(_ targets: [InspectTarget], _ sessionDir: URL) {
        guard let data = try? JSONEncoder().encode(targets) else { return }
        try? data.write(to: url(sessionDir), options: .atomic)
    }

    static func add(_ target: InspectTarget, _ sessionDir: URL) {
        save(load(sessionDir) + [target], sessionDir)
    }

    /// The newest circled target a scan has not been back to (Continue scanning aims at it).
    static func pending(_ sessionDir: URL) -> InspectTarget? {
        load(sessionDir).last { !$0.checked && $0.simd != nil }
    }

    static func markChecked(_ id: String, _ sessionDir: URL) {
        var all = load(sessionDir)
        for i in all.indices where all[i].id == id { all[i].checked = true }
        save(all, sessionDir)
    }

    /// Fits a circle to a drawn stroke: the centre of its points and their mean distance from it. A short stroke
    /// (a press) gets `pressRadius`.
    static func circle(_ stroke: [CGPoint], pressRadius: CGFloat = 44) -> (center: CGPoint, radius: CGFloat)? {
        guard !stroke.isEmpty else { return nil }
        let n = CGFloat(stroke.count)
        let c = CGPoint(x: stroke.map(\.x).reduce(0, +) / n, y: stroke.map(\.y).reduce(0, +) / n)
        let r = stroke.map { hypot($0.x - c.x, $0.y - c.y) }.reduce(0, +) / n
        return (c, max(r, r < 12 ? pressRadius : 24))
    }
}

/// The bridge from a circle drawn over the stage to the 3D view underneath: the view registers `resolve`, which
/// turns a screen circle into a point and radius on the scan (nil when no scan points fall inside it).
@MainActor
@Observable
final class StageProbe {
    @ObservationIgnored var resolve: ((CGPoint, CGFloat) -> (point: SIMD3<Float>, radius: Float)?)?
    /// The marker the hero's camera has (about 80%) landed on for one of Lea's moves; nil once it moved on (a touch,
    /// the idle drift, a reset). The stage brings in the part's real photo then (the proof hand-off) and lets it go.
    private(set) var landed: String?

    func land(_ id: String?) {
        if landed != id { landed = id }
    }
}

/// Draws the circle while the finger moves, then hands the fitted circle back. Active only in circle mode.
struct CircleDrawLayer: View {
    let done: (CGPoint, CGFloat) -> Void
    @State private var stroke: [CGPoint] = []

    var body: some View {
        Canvas { ctx, _ in
            guard stroke.count > 1 else { return }
            var p = Path()
            p.addLines(stroke)
            // The drawn stroke in `next` over a keyline, so it reads on any scan.
            ctx.stroke(p, with: .color(Palette.keyline), style: StrokeStyle(lineWidth: Line.locked + 2, lineCap: .round, lineJoin: .round))
            ctx.stroke(p, with: .color(Palette.next), style: StrokeStyle(lineWidth: Line.locked, lineCap: .round, lineJoin: .round))
        }
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { v in
                // Thin the stroke: one point every few points of movement is plenty for a circle fit.
                if let last = stroke.last, hypot(last.x - v.location.x, last.y - v.location.y) < 3 { return }
                stroke.append(v.location)
            }
            .onEnded { _ in
                if let c = InspectTargets.circle(stroke) { done(c.center, c.radius) }
                stroke = []
            })
        .accessibilityLabel("Circle an area to look closer at")
        .accessibilityAddTraits(.allowsDirectInteraction)
    }
}

/// A circled target drawn on the stage: a dashed `next` ring (dashes: not yet checked) sized to its radius on screen,
/// the same ring style as Lea's highlight.
struct TargetRing: View {
    var body: some View {
        FocusRing(style: FocusRingStyle(tone: nil, dashed: true))
    }
}
