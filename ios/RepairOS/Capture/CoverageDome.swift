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
    /// Close-ups of the box's faces at close-up resolution (small objects): 0 = top, 1 = +x, 2 = −x, 3 = +z, 4 = −z in
    /// the box's own axes (the bottom stands on the support).
    private(set) var closeFaces: Set<Int> = []
    /// How many faces need a close-up before the scan is complete (0 = none required).
    var requiredFaces = 0
    static let faceCount = 5

    var count: Int { covered.count }
    var fraction: Double { Double(covered.count) / Double(Self.cellCount) }
    var facesDone: Bool { closeFaces.count >= requiredFaces }
    /// Set by the scan when `ScanProgress` says it has enough (the dial shows done).
    var enough = false
    /// Complete: the scan has enough (`ScanProgress`), or the old full dome with every required face.
    var isDone: Bool { enough || (covered.count >= Self.doneCells && facesDone) }

    /// Azimuth sectors with a photo from the side (the two lower rings, 0–55° up) and from above (top ring).
    var sideSectors: Int { Set(covered.filter { $0 / Self.sectors < Self.rings - 1 }.map { $0 % Self.sectors }).count }
    var topSectors: Int { Set(covered.filter { $0 / Self.sectors == Self.rings - 1 }.map { $0 % Self.sectors }).count }

    /// The nearest cell that would still raise `ScanProgress`: a side sector not yet photographed (either lower
    /// ring), else a top sector, while those are short; nil when the views are complete.
    func nearestNeeded(camera: SIMD3<Float>, box: ObjectBox, side: Int = ScanProgress.sideSectors,
                       top: Int = ScanProgress.topSectors) -> Int? {
        let d = simd_normalize(camera - box.center)
        let sideHave = Set(covered.filter { $0 / Self.sectors < Self.rings - 1 }.map { $0 % Self.sectors })
        let topHave = Set(covered.filter { $0 / Self.sectors == Self.rings - 1 }.map { $0 % Self.sectors })
        var candidates: [Int] = []
        if sideHave.count < side {
            candidates = (0..<(Self.sectors * (Self.rings - 1))).filter { !sideHave.contains($0 % Self.sectors) }
        } else if topHave.count < top {
            candidates = (0..<Self.sectors).map { (Self.rings - 1) * Self.sectors + $0 }.filter { !topHave.contains($0 % Self.sectors) }
        }
        return candidates.max { simd_dot(Self.direction($0), d) < simd_dot(Self.direction($1), d) }
    }

    mutating func cover(_ cell: Int) { covered.insert(cell) }
    /// Takes a cell back (the photo that covered it was retaken).
    mutating func uncover(_ cell: Int) { covered.remove(cell) }
    mutating func uncoverFace(_ face: Int) { closeFaces.remove(face) }
    func isCovered(_ cell: Int) -> Bool { covered.contains(cell) }
    mutating func coverFace(_ face: Int) { closeFaces.insert(face) }
    func isFaceCovered(_ face: Int) -> Bool { closeFaces.contains(face) }

    /// Outward normal of a face in the box's own axes.
    static func faceNormal(_ face: Int) -> SIMD3<Float> {
        switch face {
        case 0: SIMD3(0, 1, 0)
        case 1: SIMD3(1, 0, 0)
        case 2: SIMD3(-1, 0, 0)
        case 3: SIMD3(0, 0, 1)
        default: SIMD3(0, 0, -1)
        }
    }

    /// Centre of a face, world.
    static func faceCenter(_ face: Int, box: ObjectBox) -> SIMD3<Float> {
        let r = simd_quatf(angle: box.yaw, axis: SIMD3(0, 1, 0))
        return box.center + r.act(faceNormal(face) * box.size / 2)
    }

    /// The face a point on (or near) the box surface lies on, box-local coordinates; nil for the bottom.
    static func face(local p: SIMD3<Float>, half h: SIMD3<Float>) -> Int? {
        let r = SIMD3(abs(p.x) / max(h.x, 1e-4), abs(p.y) / max(h.y, 1e-4), abs(p.z) / max(h.z, 1e-4))
        if r.y >= r.x && r.y >= r.z { return p.y > 0 ? 0 : nil }
        if r.x >= r.z { return p.x > 0 ? 1 : 2 }
        return p.z > 0 ? 3 : 4
    }

    /// The face the camera's view ray enters (nil when it misses the box or comes from below).
    static func viewFace(cameraToWorld m: simd_float4x4, box: ObjectBox) -> Int? {
        guard let d = ScanSession.rayBoxDistance(cameraToWorld: m, box: box) else { return nil }
        let cam = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let entry = cam + simd_normalize(-SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z)) * d
        let local = simd_quatf(angle: -box.yaw, axis: SIMD3(0, 1, 0)).act(entry - box.center)
        return face(local: local, half: box.size / 2)
    }

    /// The face still missing a close-up that is best turned toward the camera.
    func nearestMissingFace(camera: SIMD3<Float>, box: ObjectBox) -> Int? {
        guard requiredFaces > 0 else { return nil }
        let r = simd_quatf(angle: box.yaw, axis: SIMD3(0, 1, 0))
        return (0..<min(Self.faceCount, requiredFaces)).filter { !closeFaces.contains($0) }.max { a, b in
            simd_dot(r.act(Self.faceNormal(a)), simd_normalize(camera - Self.faceCenter(a, box: box)))
                < simd_dot(r.act(Self.faceNormal(b)), simd_normalize(camera - Self.faceCenter(b, box: box)))
        }
    }

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

    /// How much of the frame's shorter side the box spans: the larger of its projected width and height, clipped
    /// to the frame. 1 when part of the box is behind the camera (so close the object fills the view).
    func extent(_ box: ObjectBox, cameraToWorld m: simd_float4x4) -> Float {
        guard let r = rect(box, cameraToWorld: m) else { return 1 }
        return max(r.maxX - r.minX, r.maxY - r.minY) / min(width, height)
    }

    /// The box's projected bounds clipped to the frame, in pixels; nil when part of it is behind the camera.
    func rect(_ box: ObjectBox, cameraToWorld m: simd_float4x4) -> (minX: Float, minY: Float, maxX: Float, maxY: Float)? {
        let pts = box.corners.compactMap { project($0, cameraToWorld: m) }
        guard pts.count == 8 else { return nil }
        let x0 = max(0, pts.map(\.x).min()!), x1 = min(width, pts.map(\.x).max()!)
        let y0 = max(0, pts.map(\.y).min()!), y1 = min(height, pts.map(\.y).max()!)
        return (x0, y0, max(x0, x1), max(y0, y1))
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


/// One number for "how much of what the backend needs is in", 0…1, and whether that is enough to stop.
///
/// Main scan: photo views (side sectors at the orbit resolution target, `sideSectors` of 12 across the two lower
/// rings, plus `topSectors` from above), close-ups of every required face (small objects), and LiDAR keyframes from
/// enough directions for the mesh (`meshSectors` of 12 azimuth sectors).
///   with faces:    0.50 · views + 0.25 · faces + 0.25 · mesh
///   without faces: 0.65 · views + 0.35 · mesh
/// Enough at 100 %, or at `goodEnough` with every required face in.
///
/// Bottom (flip) segment: `flipSectors` underside sectors photographed, `flipMeshSectors` keyframe sectors, and the
/// line-up with the first scan: 0.6 · views + 0.2 · mesh + 0.2 · lined up. Enough when views and mesh are complete,
/// at least `flipMinKeyframes` depth keyframes are in (the server lines the bottom up by the sides both scans saw; a
/// 6-keyframe bottom only just registered), and it lined up or could not within `flipAlignWait`.
struct ScanProgress: Equatable, Sendable {
    static let sideSectors = 8
    static let topSectors = 2
    static let meshSectors = 9
    static let flipSectors = 6
    static let flipMeshSectors = 6
    static let goodEnough = 0.85
    static let flipAlignWait: TimeInterval = 25
    static let flipMinKeyframes = 15

    var views: Double
    var faces: Double?
    var mesh: Double
    var linedUp: Bool?
    var value: Double
    var enough: Bool

    var percent: Int { Int((value * 100).rounded(.down)) }

    /// - Parameter lidar: the phone records depth keyframes. Without LiDAR there are none, so the mesh share would
    ///   stay at zero and the scan could never pass 65 % (or 75 % with close-ups): the photos and close-ups carry the
    ///   whole number instead (0.65 + 0.35 and 0.5 + 0.25 + 0.25 rescaled to 1).
    static func main(dome: CoverageDome, keyframeSectors: Int, lidar: Bool = true) -> ScanProgress {
        let views = Double(min(dome.sideSectors, sideSectors) + min(dome.topSectors, topSectors)) / Double(sideSectors + topSectors)
        let mesh = lidar ? Double(min(keyframeSectors, meshSectors)) / Double(meshSectors) : 0
        let faces: Double? = dome.requiredFaces > 0 ? Double(min(dome.closeFaces.count, dome.requiredFaces)) / Double(dome.requiredFaces) : nil
        let value: Double
        if lidar {
            value = faces.map { 0.5 * views + 0.25 * $0 + 0.25 * mesh } ?? (0.65 * views + 0.35 * mesh)
        } else {
            value = faces.map { (0.5 * views + 0.25 * $0) / 0.75 } ?? views
        }
        let enough = value >= 1 - 1e-9 || (value >= goodEnough && dome.facesDone)
        return ScanProgress(views: views, faces: faces, mesh: mesh, linedUp: nil, value: value, enough: enough)
    }

    static func flip(dome: CoverageDome, keyframeSectors: Int, linedUp: Bool, elapsed: TimeInterval,
                     keyframes: Int = flipMinKeyframes) -> ScanProgress {
        let views = Double(min(dome.sideSectors + dome.topSectors, flipSectors)) / Double(flipSectors)
        let mesh = Double(min(keyframeSectors, flipMeshSectors)) / Double(flipMeshSectors)
            * min(1, Double(keyframes) / Double(flipMinKeyframes))
        let value = 0.6 * views + 0.2 * mesh + 0.2 * (linedUp ? 1 : 0)
        let covered = views >= 1 && mesh >= 1 - 1e-9
        return ScanProgress(views: views, faces: nil, mesh: mesh, linedUp: linedUp, value: value,
                            enough: covered && (linedUp || elapsed >= flipAlignWait))
    }
}

/// Which captures add information (pure, unit-tested): a still is worth taking for a new view cell, or when it is
/// clearly better than the best one there; a depth keyframe when no kept keyframe is near its viewpoint.
enum CaptureSelection {
    /// Stills per segment, at most.
    static let maxStills = 24
    /// Keyframes per segment, at most; past `keyframeSoftCap` only a keyframe from a new direction cell is kept.
    static let maxKeyframes = 60
    static let keyframeSoftCap = 45
    /// A kept keyframe within this distance and view angle makes a new one redundant.
    static let keyframeSpacing: Float = 0.05
    static let keyframeTurn: Float = 8 * .pi / 180
    /// "Clearly better": this much more resolution, or as much resolution and this much sharper.
    static let betterPx: Float = 1.25
    static let betterSharp = 1.3

    struct Best: Equatable, Sendable {
        var id: String
        var pxPerMm: Float
        var sharpness: Double
    }

    /// Before shooting: a new cell, or a clearly closer view of a covered one (predicted resolution).
    static func worthShooting(cell: Int?, predictedPx: Float?, best: [Int: Best]) -> Bool {
        guard let cell, let b = best[cell] else { return true }
        return (predictedPx ?? 0) >= b.pxPerMm * betterPx
    }

    /// After shooting: does this still beat the best one already in its cell?
    static func better(pxPerMm: Float, sharpness: Double, than b: Best) -> Bool {
        pxPerMm >= b.pxPerMm * betterPx || (pxPerMm >= b.pxPerMm * 0.95 && sharpness >= b.sharpness * betterSharp)
    }

    enum KeyframeDecision: String, Sendable { case keep, near, cap, softCap }

    /// A keyframe adds information when no kept one is within `keyframeSpacing` and `keyframeTurn` of it; past the
    /// soft cap only one from a direction cell with no keyframe yet; never past the cap.
    static func keyframe(pose m: simd_float4x4, kept: [simd_float4x4], cell: Int?, cellsWithKeyframes: Set<Int>) -> KeyframeDecision {
        if kept.count >= maxKeyframes { return .cap }
        let p = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let f = simd_normalize(-SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z))
        let redundant = kept.contains { k in
            simd_distance(p, SIMD3(k.columns.3.x, k.columns.3.y, k.columns.3.z)) < keyframeSpacing
                && acos(max(-1, min(1, simd_dot(f, simd_normalize(-SIMD3(k.columns.2.x, k.columns.2.y, k.columns.2.z)))))) < keyframeTurn
        }
        if redundant { return .near }
        if kept.count >= keyframeSoftCap, let cell, cellsWithKeyframes.contains(cell) { return .softCap }
        return .keep
    }
}
