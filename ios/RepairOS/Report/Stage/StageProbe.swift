import CoreGraphics
import Foundation

/// What the hero tells the stage about its camera.
@MainActor
final class StageProbe: ObservableObject {
    /// The marker the hero's camera has (about 80%) landed on for one of Lea's moves; nil once it moved on (a touch,
    /// the idle drift, a reset). The stage brings in the part's real photo then (the proof hand-off) and lets it go.
    @Published private(set) var landed: String?
    /// Where that part is on the hero's screen once the camera has settled, as fractions of the hero's size (0,0 top
    /// left); nil when unknown. The photo card keeps clear of it.
    @Published private(set) var landedPoint: CGPoint?

    func land(_ id: String?) {
        if landed != id { landed = id }
        if id == nil, landedPoint != nil { landedPoint = nil }
    }

    func place(_ point: CGPoint?) {
        if landedPoint != point { landedPoint = point }
    }
}
