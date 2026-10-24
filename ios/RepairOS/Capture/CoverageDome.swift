import Foundation
import simd

/// Which sides of the object have an accepted photo: 3 elevation rings above the support plane × 12 azimuth
/// sectors = 36 cells. A cell counts when an accepted still was taken from inside it at a usable distance, with
/// the object well in frame.
struct CoverageDome: Sendable, Equatable {
    static let rings = 3
    static let sectors = 12
    static let cellCount = rings * sectors
    /// Ring bounds in degrees above the plane; centres ≈ 15°, 40°, 70°.
    static let ringBounds: [Float] = [0, 27.5, 55, 90]
    static let ringCentres: [Float] = [15, 40, 70]
    static let doneCells = 24

    private(set) var covered: Set<Int> = []

    var count: Int { covered.count }
    var fraction: Double { Double(covered.count) / Double(Self.cellCount) }
    var isDone: Bool { covered.count >= Self.doneCells }

    mutating func cover(_ cell: Int) { covered.insert(cell) }
    func isCovered(_ cell: Int) -> Bool { covered.contains(cell) }

    static func id(_ cell: Int) -> String { String(format: "r%ds%02d", cell / sectors, cell % sectors) }

    /// The cell a camera at `camera` is in, looking at the box; nil below the plane.
    static func cell(camera: SIMD3<Float>, box: ObjectBox) -> Int? {
        let d = camera - box.center
        let horizontal = (d.x * d.x + d.z * d.z).squareRoot()
        let elevation = atan2(camera.y - box.planeY, horizontal) * 180 / .pi
        guard elevation >= 0 else { return nil }
        let ring = ringBounds.dropFirst().firstIndex { elevation < $0 }.map { $0 - 1 } ?? rings - 1
        return ring * sectors + sector(azimuth: azimuth(d))
    }

    static func azimuth(_ d: SIMD3<Float>) -> Float {
        var a = atan2(d.x, d.z)
        if a < 0 { a += 2 * .pi }
        return a
    }

    static func sector(azimuth: Float) -> Int { min(sectors - 1, Int(azimuth / (2 * .pi) * Float(sectors))) }

    /// Unit direction from the box centre to the middle of a cell.
    static func direction(_ cell: Int) -> SIMD3<Float> {
        let el = ringCentres[cell / sectors] * .pi / 180
        let az = (Float(cell % sectors) + 0.5) / Float(sectors) * 2 * .pi
        return SIMD3(sin(az) * cos(el), sin(el), cos(az) * cos(el))
    }

    /// The usable camera distance band for a box: [0.6, 2.5] × diagonal, never nearer than 15 cm.
    static func band(_ box: ObjectBox) -> ClosedRange<Float> {
        let near = max(0.15, 0.6 * box.diagonal)
        return near...max(near + 0.05, 2.5 * box.diagonal)
    }

    /// The uncovered cell nearest (by angle) to the camera's current direction from the object.
    func nearestMissing(camera: SIMD3<Float>, box: ObjectBox) -> Int? {
        let d = simd_normalize(camera - box.center)
        return (0..<Self.cellCount).filter { !covered.contains($0) }
            .max { simd_dot(Self.direction($0), d) < simd_dot(Self.direction($1), d) }
    }

    /// Short words that move the camera toward `cell`, in the camera's own axes (`cameraToWorld`).
    static func guidance(to cell: Int, cameraToWorld m: simd_float4x4, box: ObjectBox) -> String {
        let cam = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let here = simd_normalize(cam - box.center)
        let there = direction(cell)
        // More than ~120° around: say so instead of a long sideways walk.
        let flatHere = simd_normalize(SIMD3(here.x, 0, here.z) + 1e-6), flatThere = simd_normalize(SIMD3(there.x, 0, there.z) + 1e-6)
        if simd_dot(flatHere, flatThere) < -0.5 { return "Other side" }
        let goal = box.center + there * max(simd_length(cam - box.center), band(box).lowerBound)
        let local = simd_inverse(m) * SIMD4(goal, 1)
        if abs(local.y) > abs(local.x) { return local.y > 0 ? "Go higher" : "Go lower" }
        return local.x > 0 ? "Move right" : "Move left"
    }
}

/// Pinhole projection of the box into an image with ARKit conventions (camera looks down -Z, image v grows down).
struct Projection: Sendable {
    var fx: Float, fy: Float, cx: Float, cy: Float
    var width: Float, height: Float

    func project(_ p: SIMD3<Float>, cameraToWorld m: simd_float4x4) -> SIMD2<Float>? {
        let c = simd_inverse(m) * SIMD4(p, 1)
        guard c.z < -1e-4 else { return nil }
        return SIMD2(fx * c.x / -c.z + cx, -fy * c.y / -c.z + cy)
    }

    /// Fraction of the image the box's projected bounds cover, and whether all 8 corners are in frame.
    func fill(_ box: ObjectBox, cameraToWorld m: simd_float4x4) -> (fraction: Float, inFrame: Bool) {
        let pts = box.corners.map { project($0, cameraToWorld: m) }
        guard pts.allSatisfy({ $0 != nil }) else { return (0, false) }
        let xs = pts.map { $0!.x }, ys = pts.map { $0!.y }
        let inFrame = xs.min()! >= 0 && ys.min()! >= 0 && xs.max()! <= width && ys.max()! <= height
        let w = min(width, xs.max()!) - max(0, xs.min()!), h = min(height, ys.max()!) - max(0, ys.min()!)
        return (max(0, w) * max(0, h) / (width * height), inFrame)
    }
}

/// Is the object framed well enough for a still? Not "the whole box on screen" (impossible for big objects, and the
/// opposite of a close-up): the box centre, or at least 30 % of the box's visible area, in the middle of the frame,
/// and the LiDAR ray through the centre landing on the box. A close-up only needs that centre ray to hit the box.
enum FramingGate {
    /// The middle part of the frame: 60 % of each side.
    static let central: Float = 0.6
    static let minCentralShare: Float = 0.3
    static let hitMargin: Float = 0.02

    struct Result: Equatable, Sendable {
        var orbit: Bool
        var close: Bool
        /// nil when there is no LiDAR depth at the centre.
        var centerHitsBox: Bool?
        var centralShare: Float
    }

    static func evaluate(box: ObjectBox, cameraToWorld m: simd_float4x4, projection p: Projection,
                         centerDepth: Float?) -> Result {
        // Where the centre ray meets the scene (ARKit depth is along the camera's −Z).
        let hit: Bool? = centerDepth.map { d in
            let w = m * SIMD4<Float>(0, 0, -d, 1)
            return box.contains(SIMD3(w.x, w.y, w.z), margin: hitMargin)
        }
        let lo = SIMD2(p.width, p.height) * (1 - central) / 2, hi = SIMD2(p.width, p.height) * (1 + central) / 2
        let centerCentral = p.project(box.center, cameraToWorld: m).map { all($0 .>= lo) && all($0 .<= hi) } ?? false
        var share: Float = 0
        let pts = box.corners.compactMap { p.project($0, cameraToWorld: m) }
        if pts.count == 8 {
            let bmin = simd_max(pts.reduce(SIMD2(repeating: .greatestFiniteMagnitude)) { simd_min($0, $1) }, .zero)
            let bmax = simd_min(pts.reduce(SIMD2(repeating: -.greatestFiniteMagnitude)) { simd_max($0, $1) }, SIMD2(p.width, p.height))
            let visible = max(0, bmax.x - bmin.x) * max(0, bmax.y - bmin.y)
            let imin = simd_max(bmin, lo), imax = simd_min(bmax, hi)
            let inner = max(0, imax.x - imin.x) * max(0, imax.y - imin.y)
            share = visible > 0 ? inner / visible : 0
        } else {
            // Part of the box is behind the camera: very close, so the box fills the view.
            share = 1
        }
        let framed = centerCentral || share >= minCentralShare
        return Result(orbit: framed && (hit ?? true), close: hit ?? false, centerHitsBox: hit, centralShare: share)
    }
}
