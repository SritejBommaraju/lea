import Foundation
import simd

// "Scan the bottom": after the main scan the person flips the object and gives the phone a quick look at the
// underside (a new capture segment, its own ARKit session). The object moved, so the room's map says nothing about
// it; this registers the mini-scan's in-box cloud to the main scan's saved cloud in full 6 DoF (a flip is not a
// turn about gravity), on the phone, best effort:
//   1. both clouds as 4 mm voxel models with outward normals (ObjectModel), colours per voxel;
//   2. 24 cube orientations × 8 turns about the vertical, each placed box centre on box centre, a short coarse
//      point-to-plane ICP on few points (ObjectTracker.icp); the best few refined on more points;
//   3. ranked on overlap and RMS, with physical sense: the side that faced up in the mini-scan must face down in
//      the main scan (it was flipped), and nothing may land inside the table; colour breaks ties between poses
//      the shape cannot tell apart (a symmetric or flat object), else the attempt is refused as ambiguous;
//   4. accepted only when two attempts in a row agree (the caller's job; `agrees`).
// The same idea as ObjectAligner (Report/), freed from gravity. The backend runs its own global registration
// and has the last word.

struct FlipCloud: Sendable {
    var points: [SIMD3<Float>]
    var colors: [SIMD3<Float>]       // 0…1
    var box: TrackBox                // its locked box (its own session's world)
}

enum FlipAligner {
    struct Result: Sendable {
        /// Mini-scan frame → main scan (segment 0) frame: p0 = T · p.
        var toSegment0: simd_float4x4
        var quality: Float
        var overlap: Float
        var rmsMM: Float
        var inliers: Int
        /// How far the best beat the best distinct rival (rank units).
        var margin: Float
        var flippedDegrees: Float
        var refused: String?
        var ms: Double
        var accepted: Bool { refused == nil }
    }

    static let minInliers = 150
    static let maxRMS: Float = 0.0045
    static let minOverlap: Float = 0.18
    static let minMargin: Float = 0.04

    /// One attempt. `target`: the main scan's in-box cloud (segment-0 frame); `source`: the mini-scan's.
    static func align(target: FlipCloud, source: FlipCloud, config base: TrackerConfig = .standard) -> Result? {
        // Every direction solved: a flat object's rim pins its turn only weakly, but it is all there is.
        var config = base
        config.weakDirection = 0
        let t0 = Date()
        guard target.points.count >= 300, source.points.count >= 300 else { return nil }
        // The target as the tracker's model (ICP and scoring work on it), the source as voxel points with normals.
        let tracker = ObjectTracker(box: target.box, seed: target.points, config: config)
        guard tracker.model.count >= 200 else { return nil }
        // The underside sat on the table: the table under the box's footprint is surface (facing down) the
        // mini-scan's up-facing side must meet. Without it nothing holds the height of a flipped flat object.
        tracker.addSurface(footprint(target), normal: SIMD3(0, -1, 0))
        var srcModel = ObjectModel(box: grown(source.box))
        srcModel.seed(source.points)
        let src = ObjectTracker.Source(world: srcModel.fine.position, normal: srcModel.fine.normal,
                                       depth: [Float](repeating: 0.4, count: srcModel.fine.size), mask: [])
        guard src.world.count >= 150 else { return nil }
        let tColour = voxelColours(tracker.model.fine, target), sColour = voxelColours(srcModel.fine, source)
        let few = ObjectTracker.thin(src, max: 300), many = ObjectTracker.thin(src, max: 1500)
        let c0 = target.box.center, ck = source.box.center
        // W maps target → source (the tracker's object → world); toSegment0 = W⁻¹.
        var hyps: [(simd_float4x4, Float)] = []
        for yawStep in 0..<8 {
            let yaw = simd_float3x3(simd_quatf(angle: Float(yawStep) * .pi / 4, axis: SIMD3(0, 1, 0)))
            for c in ObjectTracker.cubeRotations {
                let r = yaw * c
                let start = Rigid.make(r, ck - r * c0)
                let out = tracker.icp(few, start: start, matches: [], schedule: [(true, 0.04), (true, 0.03), (true, 0.02), (true, 0.015)])
                hyps.append((out.pose, rank(out, start: out.pose, target: target, source: few)))
            }
        }
        hyps.sort { $0.1 > $1.1 }
        // Refine the best few distinct ones.
        var refined: [(pose: simd_float4x4, rank: Float, out: ObjectTracker.ICPOut, colour: Float)] = []
        for (pose, _) in hyps {
            if refined.count >= 6 { break }
            if refined.contains(where: { Rigid.distance($0.pose, pose, probe: target.box.corners) < 0.015 }) { continue }
            let out = tracker.icp(many, start: pose, matches: [],
                                  schedule: [(true, 0.02), (true, 0.015), (false, 0.012), (false, 0.009), (false, 0.007),
                                             (false, 0.006), (false, 0.005), (false, 0.005)])
            let col = colourDistance(tracker.model.fine, tColour, srcModel.fine, sColour, worldToObject: Rigid.inverse(out.pose))
            refined.append((out.pose, rank(out, start: out.pose, target: target, source: many), out, col))
        }
        refined.sort { $0.rank > $1.rank }
        guard var best = refined.first else { return nil }
        // Shape alone may not tell the candidates apart (a box fits on any of its faces, a flat object either way
        // round): among those close in fit, colour picks when one clearly agrees best; with no clear pick and no clear
        // fit margin the attempt is refused as ambiguous.
        let close = refined.filter { best.rank - $0.rank < 0.15 }
        let distinct = close.filter { c in !close.contains { $0.rank > c.rank && Rigid.distance($0.pose, c.pose, probe: target.box.corners) <= 0.02 } }
        var margin: Float = 1
        if distinct.count >= 2 {
            let byColour = distinct.sorted { $0.colour < $1.colour }
            if byColour[0].colour < byColour[1].colour * 0.8 {
                best = byColour[0]
                margin = max(minMargin, (1 - byColour[0].colour / byColour[1].colour) * 0.3)
            } else {
                let rivalRank = distinct.filter { Rigid.distance($0.pose, best.pose, probe: target.box.corners) > 0.02 }.map(\.rank).max() ?? -10
                margin = best.rank - rivalRank
                if margin < 0.1 { margin = min(margin, minMargin * 0.5) }
            }
        } else if let rival = refined.dropFirst().first(where: { Rigid.distance($0.pose, best.pose, probe: target.box.corners) > 0.02 }) {
            margin = best.rank - rival.rank
        }
        let W = best.pose
        let toSegment0 = Rigid.inverse(W)
        let overlap = Float(best.out.inliers) / Float(max(1, many.world.count))
        var result = Result(toSegment0: toSegment0, quality: 0, overlap: overlap, rmsMM: best.out.rms * 1000,
                            inliers: best.out.inliers, margin: margin,
                            flippedDegrees: acos(max(-1, min(1, simd_dot(Rigid.rotation(toSegment0) * SIMD3(0, 1, 0), SIMD3(0, 1, 0))))) * 180 / .pi,
                            refused: nil, ms: Date().timeIntervalSince(t0) * 1000)
        if best.out.inliers < minInliers { result.refused = "inliers" }
        else if best.out.rms > maxRMS { result.refused = "rms" }
        else if overlap < minOverlap { result.refused = "overlap" }
        else if margin < minMargin { result.refused = "ambiguous" }
        let rmsFactor = max(0, min(1, 1 - (best.out.rms - 0.002) / 0.004))
        result.quality = min(1, overlap / 0.4) * rmsFactor * min(1, 0.5 + margin * 5) * (result.accepted ? 1 : 0.5)
        return result
    }

    /// Two attempts agree: their poses put the box corners within 1.5 cm.
    static func agrees(_ a: Result, _ b: Result, box: TrackBox) -> Bool {
        Rigid.distance(a.toSegment0, b.toSegment0, probe: box.corners) < 0.015
    }

    /// Fit, overlap and physical sense. `start` is target → source (W). What faced up in the mini-scan faced down
    /// before (it was flipped): a pose that keeps it up is not considered.
    static func rank(_ out: ObjectTracker.ICPOut, start W: simd_float4x4, target: FlipCloud, source: ObjectTracker.Source) -> Float {
        let toTarget = Rigid.inverse(W)
        let share = Float(out.inliers) / Float(max(1, source.world.count))
        // (Turned onto a side counts: at least ~70°.)
        let R = Rigid.rotation(toTarget)
        if (R * SIMD3<Float>(0, 1, 0)).y > 0.35 { return -10 }
        // Nothing of the object lies inside the table it stood on, and the underside seen now (facing up) sat on it:
        // mini-scan points that land on the table plane, facing down. This is what a flat object's flip turns on
        // (its two big faces never overlap; only its rim does).
        var below = 0, onPlane = 0
        for (i, p) in source.world.enumerated() {
            let q = Rigid.apply(toTarget, p)
            if q.y < target.box.planeY - 0.008 { below += 1 }
            if abs(q.y - target.box.planeY) < 0.005, (R * source.normal[i]).y < -0.7 { onPlane += 1 }
        }
        let n = Float(max(1, source.world.count))
        return share - 20 * out.rms - 2 * Float(below) / n + 0.5 * Float(onPlane) / n
    }

    /// Mean colour per voxel of a model (nearest voxel of each raw point).
    static func voxelColours(_ level: ObjectModel.Level, _ cloud: FlipCloud) -> [SIMD3<Float>] {
        var sum = [SIMD3<Float>](repeating: .zero, count: level.size), n = [Float](repeating: 0, count: level.size)
        for (i, p) in cloud.points.enumerated() where i < cloud.colors.count {
            if let hit = level.nearest(p, maxDist: level.cell) { sum[hit.index] += cloud.colors[i]; n[hit.index] += 1 }
        }
        return (0..<level.size).map { n[$0] > 0 ? sum[$0] / n[$0] : SIMD3(repeating: -1) }
    }

    /// Mean chromaticity difference over the overlapping voxels (lower = the colours agree).
    static func colourDistance(_ t: ObjectModel.Level, _ tc: [SIMD3<Float>], _ s: ObjectModel.Level, _ sc: [SIMD3<Float>],
                               worldToObject inv: simd_float4x4) -> Float {
        func chroma(_ c: SIMD3<Float>) -> SIMD3<Float> { c / max(0.03, c.x + c.y + c.z) }
        var sum: Float = 0, n: Float = 0
        for i in 0..<s.size where sc[i].x >= 0 {
            guard let hit = t.nearest(Rigid.apply(inv, s.position[i]), maxDist: 0.006), tc[hit.index].x >= 0 else { continue }
            sum += simd_length(chroma(sc[i]) - chroma(tc[hit.index]))
            n += 1
        }
        return n > 20 ? sum / n : 1
    }

    /// Points every 4 mm on the support plane under the object: under the main scan's points (seen from above, the
    /// footprint is where they are), inside the box.
    static func footprint(_ c: FlipCloud) -> [SIMD3<Float>] {
        let cell: Float = 0.004
        var seen = Set<SIMD2<Int32>>()
        for p in c.points where p.y > c.box.planeY + 0.004 {
            seen.insert(SIMD2(Int32((p.x / cell).rounded(.down)), Int32((p.z / cell).rounded(.down))))
        }
        // Fill the footprint's outline row by row (a convex-ish object's underside), each row between its extremes.
        var rows: [Int32: (Int32, Int32)] = [:]
        for k in seen { rows[k.y] = rows[k.y].map { (min($0.0, k.x), max($0.1, k.x)) } ?? (k.x, k.x) }
        var out: [SIMD3<Float>] = []
        for (z, (x0, x1)) in rows where x1 >= x0 {
            for x in x0...x1 {
                let p = SIMD3((Float(x) + 0.5) * cell, c.box.planeY, (Float(z) + 0.5) * cell)
                if c.box.contains(p, margin: 0.002) { out.append(p) }
            }
        }
        return out
    }

    /// The mini-scan's box with room for the parts its lock cut close.
    static func grown(_ b: TrackBox) -> TrackBox { TrackBox(center: b.center, half: b.half + 0.01, yaw: b.yaw) }
}
