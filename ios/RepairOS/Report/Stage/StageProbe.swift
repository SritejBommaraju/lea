import Foundation
import Observation

/// What the hero tells the stage about its camera.
@MainActor
@Observable
final class StageProbe {
    /// The marker the hero's camera has (about 80%) landed on for one of Lea's moves; nil once it moved on (a touch,
    /// the idle drift, a reset). The stage brings in the part's real photo then (the proof hand-off) and lets it go.
    private(set) var landed: String?

    func land(_ id: String?) {
        if landed != id { landed = id }
    }
}
