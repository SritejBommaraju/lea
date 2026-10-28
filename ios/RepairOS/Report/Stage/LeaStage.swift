import Foundation
import simd

// Lea moves the report's 3D model while she answers (POST /v1/voice/converse J events): "Where's the battery
// cover?" flies the camera to it, circles it and zooms in; "show me the bottom", "turn it around", "zoom in",
// "reset". The gateway only sends parts the scan located; the phone still ignores an id it does not know. Pure
// value types and maths here (unit-tested); ReportStageView applies a plan, the hero (ModelView)
// move their cameras.

/// Which side of the object to look at. `around`: the opposite side from the camera now; `reset`: the whole object.
enum StageSide: String, Sendable, Equatable, CaseIterable {
    case top, bottom, front, back, left, right, around, reset
}

/// One move Lea asked for.
enum StageCommand: Sendable, Equatable {
    /// A located finding (the report's own marker).
    case show(String)
    /// Any other spot on the model (a part read from the photos, or one she pointed at in a photo): its point in the
    /// scan frame and a ring of `radius` metres (bigger when the spot is less certain).
    case point(id: String, at: SIMD3<Float>, label: String, radius: Float)
    case view(StageSide)
    case zoom(in: Bool)
}

/// A command for the hero's camera, numbered so the same command twice still moves it.
struct LeaStageCue: Equatable, Sendable {
    var n: Int
    var command: StageCommand
}

/// What the report does with one command: focus a marker (`focus`), add a short-lived ring (`pin`), move the hero's
/// camera (`camera`: always `.show(marker id)`, `.view` or `.zoom`, so a hero only flies to markers it has). An
/// unknown part gives an empty plan.
struct LeaStagePlan: Equatable {
    var focus: String?
    var pin: ModelPin?
    var camera: StageCommand?

    static let none = LeaStagePlan()

    /// The id a Lea pin gets, so it never collides with a finding's.
    static func pinID(_ id: String) -> String { "lea:" + id }

    /// `pins`: the report's markers on the hero now; `located`: every finding with a 3D point (the ones without an
    /// issue too: real parts she can show, in a neutral ring).
    static func make(_ command: StageCommand, pins: [ModelPin], located: [ReportModel.Problem]) -> LeaStagePlan {
        switch command {
        case .show(let id):
            if pins.contains(where: { $0.id == id }) { return LeaStagePlan(focus: id, camera: .show(id)) }
            guard let p = located.first(where: { $0.id == id }), let at = p.point else { return .none }
            let pin = ModelPin(id: pinID(id), number: nil, point: at, tone: .neutral, ringRadius: max(p.extent ?? 0, 0.012))
            return LeaStagePlan(focus: pin.id, pin: pin, camera: .show(pin.id))
        case .point(let id, let at, _, let radius):
            guard at.x.isFinite, at.y.isFinite, at.z.isFinite else { return .none }
            let pin = ModelPin(id: pinID(id), number: nil, point: at, tone: .neutral, ringRadius: min(max(radius, 0.008), 0.05))
            return LeaStagePlan(focus: pin.id, pin: pin, camera: .show(pin.id))
        case .view, .zoom:
            return LeaStagePlan(camera: command)
        }
    }
}

/// The camera poses for Lea's view and zoom moves, around the object's centre (world Y up). Directions are in the
/// object's own frame: the hero turns its model back to rest first, so "front" is the side the default framing shows.
enum StageCamera {
    /// Elevation of a side view (a little above), and of the top / bottom views (inside the ±82° orbit limits).
    static let sideElevation: Float = 0.3
    static let poleElevation: Float = 1.35
    static let zoomStep: Float = 0.6

    /// The pose for `side`, `distance` from `center`; `front` is the azimuth the default framing looks from. `around`
    /// keeps the elevation and distance of `current` and swings to the other side. nil for `reset` (the hero frames
    /// the whole object its own way).
    static func pose(_ side: StageSide, current: TourPose, center: SIMD3<Float>, distance: Float, front: Float) -> TourPose? {
        let az: Float, el: Float
        switch side {
        case .reset: return nil
        case .front: (az, el) = (front, sideElevation)
        case .back: (az, el) = (front + .pi, sideElevation)
        case .left: (az, el) = (front - .pi / 2, sideElevation)
        case .right: (az, el) = (front + .pi / 2, sideElevation)
        case .top: (az, el) = (front, poleElevation)
        case .bottom: (az, el) = (front, -poleElevation)
        case .around:
            let (r, a, e) = TourCamera.spherical(current.eye - center)
            return TourPose(eye: center + r * dir(a + .pi, e), look: center)
        }
        return TourPose(eye: center + distance * dir(az, el), look: center)
    }

    /// Closer to (or further from) what the camera looks at, by `zoomStep`, kept between `minDistance` and
    /// `maxDistance`.
    static func zoom(_ current: TourPose, zoomIn: Bool, minDistance: Float, maxDistance: Float) -> TourPose {
        let v = current.eye - current.look
        let d = max(simd_length(v), 1e-5)
        let next = min(maxDistance, max(minDistance, zoomIn ? d * zoomStep : d / zoomStep))
        return TourPose(eye: current.look + v / d * next, look: current.look)
    }

    /// Unit direction from azimuth (about +Y from +Z) and elevation.
    static func dir(_ az: Float, _ el: Float) -> SIMD3<Float> {
        SIMD3(cos(el) * sin(az), sin(el), cos(el) * cos(az))
    }

    /// The azimuth of a framing direction (e.g. ModelView's default (0.35, 0.55, 1)).
    static func azimuth(_ v: SIMD3<Float>) -> Float { atan2(v.x, v.z) }
}

/// When a move should happen so it lands with the words it belongs to: the gateway sends it right after its
/// sentence's text, but that sentence is heard later, once the ones before it have been spoken.
enum StageTiming {
    /// Seconds to wait: from when the first sentence started being heard, the time to say the characters before this
    /// sentence (at `charsPerSecond`), minus the time already passed. Never negative.
    static func delay(firstHeardAt: Date?, charsBefore: Int, now: Date, charsPerSecond: Double = 14) -> Double {
        guard let start = firstHeardAt else { return 0 }
        return max(0, start.timeIntervalSince(now) + Double(charsBefore) / charsPerSecond)
    }
}
