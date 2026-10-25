import Accelerate
import Foundation
import simd

// Object-level alignment for "Show on object": registers the live LiDAR points of the object (seen from wherever the
// person stands) to the scan's saved in-box cloud, so the markers land without the ARWorldMap having to recognise
// the room. Both sessions are gravity-aligned (worldAlignment .gravity), so the unknowns are a yaw about the vertical
// and a translation (4 DOF):
//   1. coarse: the support planes give the height; the live object's footprint (shifted away from the camera, since
//      only the near side is seen) gives the horizontal start; 24 yaws × 2 shifts are tried with a short ICP;
//   2. fine: point-to-plane ICP (4 DOF, trimmed correspondences) on the best few, then acceptance: the live points'
//      inlier share, their RMS, how much of the saved object they cover, and no near-equal second answer (a
//      symmetric object is resolved by colour, or the attempt is refused and the next one waits for more sides).

/// The saved object, prepared once: points (≤ ~6k, voxel-averaged), normals, chromaticity, and a lookup grid.
struct AlignTarget: Sendable {
    var points: [SIMD3<Float>]
    var normals: [SIMD3<Float>]
    var chroma: [SIMD3<Float>]
    var grid: VoxelGrid
    /// 3 cm cells, for correspondences searched farther than 1 cm (the coarse passes).
    var wide: VoxelGrid
    /// Footprint centre (x, z), support plane height, horizontal reach (90th percentile), height above the plane.
    var center: SIMD3<Float>
    var planeY: Float
    var reach: Float
    var height: Float

    /// `planeY`: the scan's support plane (the crop box bottom); points within 8 mm of it are dropped.
    static func make(points raw: [SIMD3<Float>], colors: [SIMD3<Float>], planeY: Float?) -> AlignTarget? {
        let floor = planeY ?? (raw.map(\.y).min() ?? 0)
        var keepP: [SIMD3<Float>] = [], keepC: [SIMD3<Float>] = []
        for (i, p) in raw.enumerated() where p.y > floor + 0.008 {
            keepP.append(p)
            keepC.append(i < colors.count ? colors[i] : SIMD3(repeating: 0.5))
        }
        guard keepP.count >= 200 else { return nil }
        var cell: Float = 0.004
        var (p, c) = Self.voxelAverage(keepP, keepC, cell: cell)
        while p.count > 6000 { cell *= 1.3; (p, c) = Self.voxelAverage(keepP, keepC, cell: cell) }
        let grid = VoxelGrid(p, cell: 0.012)
        let mean = p.reduce(.zero, +) / Float(p.count)
        // Oriented outward from the object's middle (good enough for the visibility check on everyday shapes).
        let normals = p.indices.map { i -> SIMD3<Float> in
            let n = grid.normal(at: p[i], points: p, radius: 0.012)
            return simd_dot(n, p[i] - mean) < 0 ? -n : n
        }
        var cx: Float = 0, cz: Float = 0
        for q in p { cx += q.x; cz += q.z }
        cx /= Float(p.count); cz /= Float(p.count)
        let reach = Self.percentile(p.map { simd_length(SIMD2($0.x - cx, $0.z - cz)) }, 0.9)
        let top = Self.percentile(p.map(\.y), 0.98)
        return AlignTarget(points: p, normals: normals, chroma: c.map(Self.chromaticity), grid: VoxelGrid(p, cell: 0.01),
                           wide: VoxelGrid(p, cell: 0.03),
                           center: SIMD3(cx, floor, cz), planeY: floor, reach: max(0.02, reach), height: max(0.01, top - floor))
    }

    static func voxelAverage(_ p: [SIMD3<Float>], _ c: [SIMD3<Float>], cell: Float) -> ([SIMD3<Float>], [SIMD3<Float>]) {
        var acc: [SIMD3<Int32>: (SIMD3<Float>, SIMD3<Float>, Float)] = [:]
        for (i, q) in p.enumerated() {
            let k = VoxelGrid.key(q, cell)
            let e = acc[k] ?? (.zero, .zero, 0)
            acc[k] = (e.0 + q, e.1 + c[i], e.2 + 1)
        }
        return (acc.values.map { $0.0 / $0.2 }, acc.values.map { $0.1 / $0.2 })
    }

    static func percentile(_ v: [Float], _ q: Double) -> Float {
        guard !v.isEmpty else { return 0 }
        let s = v.sorted()
        return s[Int(Double(s.count - 1) * q)]
    }

    /// rgb / (r + g + b): robust to the light being brighter or dimmer than during the scan.
    static func chromaticity(_ rgb: SIMD3<Float>) -> SIMD3<Float> { rgb / max(0.03, rgb.x + rgb.y + rgb.z) }
}

/// A uniform hash grid over points: nearest neighbour within one cell's reach, neighbourhoods for normals.
struct VoxelGrid: Sendable {
    let cell: Float
    private var cells: [SIMD3<Int32>: [Int32]] = [:]
    private let points: [SIMD3<Float>]

    init(_ points: [SIMD3<Float>], cell: Float) {
        self.cell = cell
        self.points = points
        for (i, p) in points.enumerated() { cells[Self.key(p, cell), default: []].append(Int32(i)) }
    }

    static func key(_ p: SIMD3<Float>, _ cell: Float) -> SIMD3<Int32> {
        SIMD3(Int32((p.x / cell).rounded(.down)), Int32((p.y / cell).rounded(.down)), Int32((p.z / cell).rounded(.down)))
    }

    /// The nearest point within `maxDist` (≤ `cell`), and its squared distance.
    func nearest(_ q: SIMD3<Float>, maxDist: Float) -> (index: Int, d2: Float)? {
        let k = Self.key(q, cell)
        var best = -1
        var bestD2 = maxDist * maxDist
        for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 {
            guard let list = cells[k &+ SIMD3(dx, dy, dz)] else { continue }
            for i in list {
                let d2 = simd_distance_squared(points[Int(i)], q)
                if d2 < bestD2 { bestD2 = d2; best = Int(i) }
            }
        } } }
        return best >= 0 ? (best, bestD2) : nil
    }

    /// Surface normal at `q`: the smallest-eigenvalue direction of the neighbours' covariance (PCA).
    func normal(at q: SIMD3<Float>, points p: [SIMD3<Float>], radius: Float) -> SIMD3<Float> {
        let k = Self.key(q, cell)
        var nb: [SIMD3<Float>] = []
        for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 {
            for i in cells[k &+ SIMD3(dx, dy, dz)] ?? [] where simd_distance(p[Int(i)], q) <= radius { nb.append(p[Int(i)]) }
        } } }
        guard nb.count >= 4 else { return SIMD3(0, 1, 0) }
        let m = nb.reduce(.zero, +) / Float(nb.count)
        var c = simd_float3x3()
        for v in nb {
            let d = v - m
            c += simd_float3x3(columns: (d * d.x, d * d.y, d * d.z))
        }
        return Self.smallestEigenvector(c)
    }

    /// Jacobi rotations on a symmetric 3×3.
    static func smallestEigenvector(_ m: simd_float3x3) -> SIMD3<Float> {
        var a = [[m[0][0], m[1][0], m[2][0]], [m[0][1], m[1][1], m[2][1]], [m[0][2], m[1][2], m[2][2]]].map { $0.map(Double.init) }
        var v: [[Double]] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        for _ in 0..<12 {
            for (p, q) in [(0, 1), (0, 2), (1, 2)] where abs(a[p][q]) > 1e-15 {
                let theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                let t = (theta >= 0 ? 1.0 : -1.0) / (abs(theta) + (theta * theta + 1).squareRoot())
                let c = 1 / (t * t + 1).squareRoot(), s = t * c
                for k in 0..<3 {
                    let akp = a[k][p], akq = a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                }
                for k in 0..<3 {
                    let apk = a[p][k], aqk = a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                }
                for k in 0..<3 {
                    let vkp = v[k][p], vkq = v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq
                }
            }
        }
        let i = (0..<3).min { a[$0][$0] < a[$1][$1] } ?? 0
        let n = SIMD3(Float(v[0][i]), Float(v[1][i]), Float(v[2][i]))
        return simd_length(n) > 1e-6 ? simd_normalize(n) : SIMD3(0, 1, 0)
    }
}

/// Live points: world position (this session's frame) and RGB 0…1.
struct LivePoint: Sendable { var p: SIMD3<Float>; var rgb: SIMD3<Float> }

enum ObjectAligner {
    struct Result: Sendable {
        /// Saved-scan frame → this session's world frame (rotation about +Y, then translation).
        var savedToLive: simd_float4x4
        var rms: Float
        var inliers: Float
        var coverage: Float
        var livePoints: Int
        var yawDegrees: Float
        /// Share of the saved surface facing the camera that the live points explain.
        var explained: Float = 0
        /// Why it was refused, nil when accepted.
        var refused: String?
        var accepted: Bool { refused == nil }
    }

    static let maxRMS: Float = 0.0065
    static let minInliers: Float = 0.62
    static let minCoverage: Float = 0.22
    static let inlierDist: Float = 0.009
    static let pointWeight: Double = 0.35
    static let minExplained: Float = 0.5

    /// One attempt. `live`: points around the view (this session's frame); `aim`: where the view centre meets the
    /// scene; `planeY`: the live support plane (ARKit's horizontal plane under the aim), nil to estimate it;
    /// `camera`: the camera position (the near side of the object faces it).
    static func align(target t: AlignTarget, live all: [LivePoint], aim: SIMD3<Float>, planeY livePlane: Float?,
                      camera: SIMD3<Float>) -> Result? {
        guard let plane = livePlane ?? estimatePlane(all, below: aim) else { return nil }
        guard let object = segment(all, aim: aim, planeY: plane, target: t), object.count >= 250 else { return nil }
        var cL = SIMD3<Float>(0, plane, 0)
        for o in object { cL.x += o.p.x; cL.z += o.p.z }
        cL.x /= Float(object.count); cL.z /= Float(object.count)
        var away = SIMD2(cL.x - camera.x, cL.z - camera.z)
        away = simd_length(away) > 1e-4 ? simd_normalize(away) : SIMD2(0, 1)
        let fine = thin(object, max: 2500)
        let mid = thin(object, max: 1000)
        let coarse = thin(object, max: 400)

        // Coarse: 24 yaws × 2 start shifts, a short ICP each.
        var tries: [(yaw: Float, aL: SIMD3<Float>, trans: SIMD3<Float>, score: Float)] = []
        for shift in [Float(0.15), 0.5] {
            let aL = cL + SIMD3(away.x, 0, away.y) * (shift * t.reach)
            for k in 0..<24 {
                var yaw = Float(k) * .pi / 12
                var trans = SIMD3<Float>.zero
                icp(t, coarse, aL: aL, yaw: &yaw, trans: &trans, iterations: 10, maxDist: 0.03)
                let s = score(t, coarse, aL: aL, yaw: yaw, trans: trans)
                tries.append((yaw, aL, trans, s.inliers - 20 * s.rms))
            }
        }
        tries.sort { $0.score > $1.score }
        // Fine: the best few, refined; distinct answers kept apart for the ambiguity check.
        var refined: [(yaw: Float, aL: SIMD3<Float>, trans: SIMD3<Float>, rms: Float, inl: Float, cov: Float, colour: Float,
                       explained: Float)] = []
        // ICP creeps along flat faces (≈ 100 iterations from 3 cm off), so many cheap iterations on 1000 points.
        for c in tries.prefix(5) {
            var yaw = c.yaw, trans = c.trans
            icp(t, mid, aL: c.aL, yaw: &yaw, trans: &trans, iterations: 30, maxDist: 0.02)
            icp(t, mid, aL: c.aL, yaw: &yaw, trans: &trans, iterations: 80, maxDist: 0.01)
            icp(t, fine, aL: c.aL, yaw: &yaw, trans: &trans, iterations: 8, maxDist: 0.007)
            let s = score(t, fine, aL: c.aL, yaw: yaw, trans: trans)
            refined.append((yaw, c.aL, trans, s.rms, s.inliers, coverage(t, fine, aL: c.aL, yaw: yaw, trans: trans), s.colour,
                            explained(t, fine, aL: c.aL, yaw: yaw, trans: trans, camera: camera, aim: aim)))
        }
        // Rank by fit and by how much of what this camera should see is really there.
        func rank(_ r: (yaw: Float, aL: SIMD3<Float>, trans: SIMD3<Float>, rms: Float, inl: Float, cov: Float, colour: Float,
                        explained: Float)) -> Float { r.inl + r.explained - 20 * r.rms }
        refined.sort { rank($0) > rank($1) }
        guard var best = refined.first else { return nil }
        // Polish the winner: ICP creeps, so a long run on it alone.
        do {
            var yaw = best.yaw, trans = best.trans
            icp(t, mid, aL: best.aL, yaw: &yaw, trans: &trans, iterations: 150, maxDist: 0.008)
            let s = score(t, fine, aL: best.aL, yaw: yaw, trans: trans)
            best = (yaw, best.aL, trans, s.rms, s.inliers, coverage(t, fine, aL: best.aL, yaw: yaw, trans: trans), s.colour,
                    explained(t, fine, aL: best.aL, yaw: yaw, trans: trans, camera: camera, aim: aim))
        }
        let m = matrix(aL: best.aL, aS: t.center, yaw: best.yaw, trans: best.trans)
        var result = Result(savedToLive: m, rms: best.rms, inliers: best.inl, coverage: best.cov, livePoints: object.count,
                            yawDegrees: wrap(best.yaw) * 180 / .pi, refused: nil)
        if best.inl < minInliers { result.refused = "inliers" } else if best.rms > maxRMS { result.refused = "rms" }
        else if best.cov < minCoverage { result.refused = "coverage" }
        else if best.explained < minExplained { result.refused = "unexplained" }
        result.explained = best.explained
        // A different pose that fits about as well (a symmetric object): colour decides, or refuse.
        let mBest = m
        if result.accepted, let rival = refined.dropFirst().first(where: { r in
            let mr = matrix(aL: r.aL, aS: t.center, yaw: r.yaw, trans: r.trans)
            return poseDistance(mBest, mr, at: t.center, reach: t.reach) > 0.04
        }), rank(rival) >= rank(best) - 0.08 {
            if !(best.colour < rival.colour * 0.8) { result.refused = "ambiguous" }
        }
        return result
    }

    // MARK: Pieces

    /// The support plane under the aim: the most populated 5 mm height band below it.
    static func estimatePlane(_ pts: [LivePoint], below aim: SIMD3<Float>) -> Float? {
        var bins: [Int: Int] = [:]
        for q in pts where q.p.y <= aim.y + 0.005 && simd_length(SIMD2(q.p.x - aim.x, q.p.z - aim.z)) < 0.5 {
            bins[Int((q.p.y / 0.005).rounded(.down)), default: 0] += 1
        }
        guard let top = bins.max(by: { $0.value < $1.value }), top.value >= 80 else { return nil }
        return (Float(top.key) + 0.5) * 0.005
    }

    /// The object under the aim: points above the plane, within the saved object's size of the aim, the connected
    /// piece (1.5 cm cells) nearest the aim.
    static func segment(_ pts: [LivePoint], aim: SIMD3<Float>, planeY: Float, target t: AlignTarget) -> [LivePoint]? {
        let r = 2 * t.reach + 0.03
        let topY = planeY + 1.3 * t.height + 0.02
        let cand = pts.filter { $0.p.y > planeY + 0.008 && $0.p.y < topY && simd_length(SIMD2($0.p.x - aim.x, $0.p.z - aim.z)) < r }
        guard cand.count >= 150 else { return nil }
        let cell: Float = 0.015
        var cells: [SIMD3<Int32>: [Int]] = [:]
        for (i, q) in cand.enumerated() { cells[VoxelGrid.key(q.p, cell), default: []].append(i) }
        // Seed: the occupied cell nearest the aim.
        guard let seed = cells.keys.min(by: {
            simd_distance(SIMD3<Float>($0) * cell, aim) < simd_distance(SIMD3<Float>($1) * cell, aim)
        }) else { return nil }
        var seen: Set<SIMD3<Int32>> = [seed], queue = [seed], out: [LivePoint] = []
        while let k = queue.popLast() {
            for i in cells[k] ?? [] { out.append(cand[i]) }
            for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 {
                let n = k &+ SIMD3(dx, dy, dz)
                if cells[n] != nil, seen.insert(n).inserted { queue.append(n) }
            } } }
        }
        return out
    }

    static func thin(_ p: [LivePoint], max n: Int) -> [LivePoint] {
        guard p.count > n else { return p }
        let step = Double(p.count) / Double(n)
        return (0..<n).map { p[Int(Double($0) * step)] }
    }

    static func rotY(_ a: Float) -> simd_float3x3 {
        let c = cos(a), s = sin(a)
        return simd_float3x3(columns: (SIMD3(c, 0, -s), SIMD3(0, 1, 0), SIMD3(s, 0, c)))
    }

    /// live → saved: q = Ry(yaw)(p − aL) + aS + trans.
    @inline(__always)
    static func toSaved(_ p: SIMD3<Float>, aL: SIMD3<Float>, aS: SIMD3<Float>, r: simd_float3x3, trans: SIMD3<Float>) -> SIMD3<Float> {
        r * (p - aL) + aS + trans
    }

    @inline(__always)
    static func nearest(_ t: AlignTarget, _ q: SIMD3<Float>, maxDist: Float) -> (index: Int, d2: Float)? {
        (maxDist <= t.grid.cell ? t.grid : t.wide).nearest(q, maxDist: maxDist)
    }

    /// Point-to-plane ICP over yaw + translation, trimmed at `maxDist`.
    static func icp(_ t: AlignTarget, _ live: [LivePoint], aL: SIMD3<Float>, yaw: inout Float, trans: inout SIMD3<Float>,
                    iterations: Int, maxDist: Float) {
        for _ in 0..<iterations {
            let r = rotY(yaw)
            var ata = [Double](repeating: 0, count: 16), atb = [Double](repeating: 0, count: 4)
            var used = 0
            for l in live {
                let q = toSaved(l.p, aL: aL, aS: t.center, r: r, trans: trans)
                guard let hit = nearest(t, q, maxDist: maxDist) else { continue }
                let s = t.points[hit.index], n = t.normals[hit.index]
                let d = q - t.center
                // Point-to-plane, plus a weaker point-to-point term that stops sliding along flat faces.
                let rows: [([Double], Double)] = [
                    ([Double(simd_dot(n, SIMD3(d.z, 0, -d.x))), Double(n.x), Double(n.y), Double(n.z)], Double(simd_dot(n, q - s))),
                    ([Double(d.z) * pointWeight, pointWeight, 0, 0], Double(q.x - s.x) * pointWeight),
                    ([0, 0, pointWeight, 0], Double(q.y - s.y) * pointWeight),
                    ([Double(-d.x) * pointWeight, 0, 0, pointWeight], Double(q.z - s.z) * pointWeight),
                ]
                for (j, res) in rows {
                    for a in 0..<4 {
                        atb[a] -= j[a] * res
                        for b in 0..<4 { ata[a * 4 + b] += j[a] * j[b] }
                    }
                }
                used += 1
            }
            guard used >= 30 else { return }
            for a in 0..<4 { ata[a * 4 + a] += 1e-6 * Double(used) }
            guard let x = solve4(ata, atb) else { return }
            let dYaw = Float(x[0]), dT = SIMD3(Float(x[1]), Float(x[2]), Float(x[3]))
            yaw += dYaw
            trans = rotY(dYaw) * trans + dT
            if abs(dYaw) < 2e-5 && simd_length(dT) < 2e-5 { return }
        }
    }

    /// Gaussian elimination with partial pivoting on a 4×4 (row-major).
    static func solve4(_ m: [Double], _ b: [Double]) -> [Double]? {
        var a = (0..<4).map { r in (0..<4).map { m[r * 4 + $0] } + [b[r]] }
        for c in 0..<4 {
            guard let p = (c..<4).max(by: { abs(a[$0][c]) < abs(a[$1][c]) }), abs(a[p][c]) > 1e-12 else { return nil }
            a.swapAt(c, p)
            for r in 0..<4 where r != c {
                let f = a[r][c] / a[c][c]
                for k in c..<5 { a[r][k] -= f * a[c][k] }
            }
        }
        return (0..<4).map { a[$0][4] / a[$0][$0] }
    }

    static func score(_ t: AlignTarget, _ live: [LivePoint], aL: SIMD3<Float>, yaw: Float, trans: SIMD3<Float>)
        -> (rms: Float, inliers: Float, colour: Float) {
        let r = rotY(yaw)
        var n = 0, sum: Float = 0, col: Float = 0
        for l in live {
            let q = toSaved(l.p, aL: aL, aS: t.center, r: r, trans: trans)
            guard let hit = t.grid.nearest(q, maxDist: inlierDist) else { continue }
            n += 1
            sum += hit.d2
            col += simd_length(AlignTarget.chromaticity(l.rgb) - t.chroma[hit.index])
        }
        guard n > 0 else { return (1, 0, 1) }
        return ((sum / Float(n)).squareRoot(), Float(n) / Float(max(1, live.count)), col / Float(n))
    }

    /// Share of the saved object (its points) with a live point within 1 cm: how much of it the view explains.
    static func coverage(_ t: AlignTarget, _ live: [LivePoint], aL: SIMD3<Float>, yaw: Float, trans: SIMD3<Float>) -> Float {
        let r = rotY(yaw)
        let moved = live.map { toSaved($0.p, aL: aL, aS: t.center, r: r, trans: trans) }
        let grid = VoxelGrid(moved, cell: 0.01)
        let hits = t.points.filter { grid.nearest($0, maxDist: 0.01) != nil }.count
        return Float(hits) / Float(max(1, t.points.count))
    }

    /// Of the saved points this camera should see (facing it, within ~30° of the view axis), the share with a live
    /// point within 1 cm. A wrong pose leaves surfaces the camera should see unexplained.
    static func explained(_ t: AlignTarget, _ live: [LivePoint], aL: SIMD3<Float>, yaw: Float, trans: SIMD3<Float>,
                          camera: SIMD3<Float>, aim: SIMD3<Float>) -> Float {
        let r = rotY(yaw)
        let cam = toSaved(camera, aL: aL, aS: t.center, r: r, trans: trans)
        let axis = simd_normalize(toSaved(aim, aL: aL, aS: t.center, r: r, trans: trans) - cam)
        let grid = VoxelGrid(live.map { toSaved($0.p, aL: aL, aS: t.center, r: r, trans: trans) }, cell: 0.01)
        var expected = 0, seen = 0
        for (i, q) in t.points.enumerated() {
            let to = simd_normalize(cam - q)
            guard simd_dot(t.normals[i], to) > 0.3, simd_dot(-to, axis) > 0.86 else { continue }
            expected += 1
            if grid.nearest(q, maxDist: 0.01) != nil { seen += 1 }
        }
        return expected < 50 ? 0 : Float(seen) / Float(expected)
    }

    /// saved → live: p = Ry(−yaw)(q − aS − trans) + aL.
    static func matrix(aL: SIMD3<Float>, aS: SIMD3<Float>, yaw: Float, trans: SIMD3<Float>) -> simd_float4x4 {
        let r = rotY(-yaw)
        let o = aL - r * (aS + trans)
        return simd_float4x4(columns: (SIMD4(r.columns.0, 0), SIMD4(r.columns.1, 0), SIMD4(r.columns.2, 0), SIMD4(o, 1)))
    }

    /// How far apart two poses put the object: the larger move of its centre or of a point at its reach.
    static func poseDistance(_ a: simd_float4x4, _ b: simd_float4x4, at c: SIMD3<Float>, reach: Float) -> Float {
        [c, c + SIMD3(reach, 0, 0), c + SIMD3(0, 0, reach)].map { o in
            simd_distance((a * SIMD4(o, 1)).xyz, (b * SIMD4(o, 1)).xyz)
        }.max() ?? 0
    }

    static func wrap(_ a: Float) -> Float {
        var x = a.truncatingRemainder(dividingBy: 2 * .pi)
        if x > .pi { x -= 2 * .pi } else if x < -.pi { x += 2 * .pi }
        return x
    }
}

extension SIMD4 where Scalar == Float {
    var xyz: SIMD3<Float> { SIMD3(x, y, z) }
}
