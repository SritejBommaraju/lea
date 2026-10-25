import Foundation
import simd

// Tier 1 geometry: the object AND the patch of table it stands on, from the scan's keyframe LiDAR depth, on the phone.
// The same steps as the gateway's `repairos/recreation/surface.py` (keep the two in step):
//   the table height under the box (lowest well-supported horizontal layer of depth points),
//   a dense TSDF over the box grown sideways (high confidence weight 1, medium 0.4; truncation >= 1.2 cm, the LiDAR's
//   noise), outside the box's footprint only the table is kept (a mug beside the object is not part of it),
//   naive Surface Nets on the zero level (even triangles, which texture well), the piece under the box centre,
//   Taubin smoothing (no shrinking).
// Why the table: cutting at the table + its noise (the report's old mesh) removes the lower centimetre of every object
// and leaves a flat one (a controller, a charger) as a few hundred triangles in the air.

struct DepthFrameData: Sendable {
    var pose: CameraPose
    var cam: Intrinsics            // of the depth grid
    var width: Int
    var height: Int
    var depth: [Float]             // metres, 0 = none
    var confidence: [UInt8]?       // ARKit 0/1/2
}

/// Camera-to-world, ARKit camera axes (the camera looks down −Z, image y down = camera −Y).
struct CameraPose: Sendable {
    var rotation: simd_float3x3
    var position: SIMD3<Float>

    init(position: SIMD3<Float>, quaternionXYZW q: [Float]) {
        self.position = position
        rotation = simd_float3x3(simd_quatf(ix: q[0], iy: q[1], iz: q[2], r: q[3]).normalized)
    }

    @inline(__always) func toCamera(_ p: SIMD3<Float>) -> SIMD3<Float> { rotation.transpose * (p - position) }
    @inline(__always) func toWorld(_ p: SIMD3<Float>) -> SIMD3<Float> { rotation * p + position }
}

struct Intrinsics: Sendable {
    var fx: Float, fy: Float, cx: Float, cy: Float
    var width: Int, height: Int

    func scaled(to w: Int, _ h: Int) -> Intrinsics {
        let sx = Float(w) / Float(width), sy = Float(h) / Float(height)
        return Intrinsics(fx: fx * sx, fy: fy * sy, cx: cx * sx, cy: cy * sy, width: w, height: h)
    }

    /// Pixel (x right, y down) and depth along the view axis of a camera-frame point.
    @inline(__always) func project(_ pc: SIMD3<Float>) -> (SIMD2<Float>, Float) {
        let z = -pc.z
        return (SIMD2(cx + fx * pc.x / z, cy - fy * pc.y / z), z)
    }
}

/// The locked object box (scan_package `crop_box`): centre, side lengths, yaw-only rotation.
struct ShellBox: Sendable {
    var center: SIMD3<Float>
    var extent: SIMD3<Float>
    var rotation: simd_float3x3     // box axes in the world (columns), unscaled

    func local(_ p: SIMD3<Float>) -> SIMD3<Float> { rotation.transpose * (p - center) }
    var radius: Float { simd_length(extent) / 2 }
}

enum ShellSurface {
    static let grow: Float = 0.35, growMin: Float = 0.04
    static let below: Float = 0.015, above: Float = 0.03
    static let truncVoxels: Float = 3, truncMin: Float = 0.012
    static let minWeight: Float = 1.5
    static let voxelLimits: ClosedRange<Float> = 0.0025...0.006
    static let maxVoxels = 2_500_000
    static let footprintMargin: Float = 0.1, tableBand: Float = 0.012

    struct Grid: Sendable {
        var lo: SIMD3<Float>          // box-local position of voxel (0,0,0)'s corner
        var voxel: Float
        var nx: Int, ny: Int, nz: Int
        var box: ShellBox

        @inline(__always) func world(_ i: Float, _ j: Float, _ k: Float) -> SIMD3<Float> {
            box.rotation * (lo + (SIMD3(i, j, k) + 0.5) * voxel) + box.center
        }
        @inline(__always) func index(_ i: Int, _ j: Int, _ k: Int) -> Int { (i * ny + j) * nz + k }
    }

    struct Result: Sendable {
        var positions: [SIMD3<Float>]
        var indices: [UInt32]
        var groundY: Float?
        var voxel: Float
        var seconds: [String: Double]
    }

    // MARK: Table height

    /// The lowest well-supported horizontal layer (band 4 mm, at least 3 % of the points, at least half the best
    /// layer's support) of high-confidence depth points in the box's grown footprint between 10 cm below the box and
    /// its middle (find_support_plane in the backend).
    static func groundHeight(_ frames: [DepthFrameData], box: ShellBox) -> Float? {
        let lo = box.center.y - box.extent.y / 2 - 0.10, hi = box.center.y
        var ys: [Float] = []
        let step = max(1, frames.count / 30)
        for f in Swift.stride(from: 0, to: frames.count, by: step).map({ frames[$0] }) {
            for v in Swift.stride(from: 0, to: f.height, by: 2) {
                for u in Swift.stride(from: 0, to: f.width, by: 2) {
                    let i = v * f.width + u
                    let d = f.depth[i]
                    guard d > 0, (f.confidence?[i] ?? 2) >= 2 else { continue }
                    let pc = SIMD3<Float>((Float(u) + 0.5 - f.cam.cx) / f.cam.fx * d, -(Float(v) + 0.5 - f.cam.cy) / f.cam.fy * d, -d)
                    let p = f.pose.toWorld(pc)
                    guard p.y > lo, p.y < hi else { continue }
                    let l = box.local(p)
                    if abs(l.x) <= box.extent.x * (0.5 + grow), abs(l.z) <= box.extent.z * (0.5 + grow) { ys.append(p.y) }
                }
            }
        }
        guard ys.count >= 50 else { return nil }
        ys.sort()
        let band: Float = 0.004
        var counts = [Int](repeating: 0, count: ys.count)
        var a = 0, b = 0
        for i in 0..<ys.count {
            while ys[a] < ys[i] - band { a += 1 }
            while b < ys.count && ys[b] <= ys[i] + band { b += 1 }
            counts[i] = b - a
        }
        let best = counts.max() ?? 0
        guard Float(best) >= 0.03 * Float(ys.count) else { return nil }
        guard let first = counts.firstIndex(where: { Float($0) >= 0.5 * Float(best) }) else { return nil }
        // the densest centre within the band of that first strong layer
        var j = first, k = first
        while k + 1 < ys.count && ys[k + 1] - ys[first] <= band { k += 1 }
        for i in first...k where counts[i] > counts[j] { j = i }
        return ys[j]
    }

    // MARK: Grid + fusion

    static func grid(box: ShellBox, groundY: Float?) -> Grid {
        let side = max(box.extent.x, box.extent.z)
        let g = max(grow * side, growMin)
        let bottom = (groundY ?? (box.center.y - box.extent.y / 2)) - below
        let top = box.center.y + box.extent.y / 2 + above
        let lo = SIMD3<Float>(-box.extent.x / 2 - g, bottom - box.center.y, -box.extent.z / 2 - g)
        let hi = SIMD3<Float>(box.extent.x / 2 + g, top - box.center.y, box.extent.z / 2 + g)
        let size = hi - lo
        var voxel = min(max(simd_length(box.extent) / 160, voxelLimits.lowerBound), voxelLimits.upperBound)
        func count(_ v: Float) -> Int { Int(ceil(size.x / v)) * Int(ceil(size.y / v)) * Int(ceil(size.z / v)) }
        while count(voxel) > maxVoxels { voxel *= 1.1 }
        return Grid(lo: lo, voxel: voxel, nx: Int(ceil(size.x / voxel)), ny: Int(ceil(size.y / voxel)),
                    nz: Int(ceil(size.z / voxel)), box: box)
    }

    /// Projective TSDF, running weighted mean, parallel over x slabs (every voxel reads every frame).
    static func fuse(_ frames: [DepthFrameData], grid: Grid) -> (tsdf: [Float], weight: [Float]) {
        let n = grid.nx * grid.ny * grid.nz
        var tsdf = [Float](repeating: 1, count: n)
        var weight = [Float](repeating: 0, count: n)
        let trunc = max(truncVoxels * grid.voxel, truncMin)
        // Per frame: world -> camera as one affine map, so a voxel costs a few multiply-adds.
        let maps = frames.map { f -> (simd_float3x3, SIMD3<Float>) in
            let rt = f.pose.rotation.transpose
            return (rt, -(rt * f.pose.position))
        }
        tsdf.withUnsafeMutableBufferPointer { tp in
            weight.withUnsafeMutableBufferPointer { wp in
                let t = tp, w = wp
                DispatchQueue.concurrentPerform(iterations: grid.nx) { i in
                    for j in 0..<grid.ny {
                        for k in 0..<grid.nz {
                            let p = grid.world(Float(i), Float(j), Float(k))
                            let idx = grid.index(i, j, k)
                            var ts = t[idx], ws = w[idx]
                            for (fi, f) in frames.enumerated() {
                                let pc = maps[fi].0 * p + maps[fi].1
                                let z = -pc.z
                                guard z > 0.05 else { continue }
                                let u = Int(f.cam.cx + f.cam.fx * pc.x / z), v = Int(f.cam.cy - f.cam.fy * pc.y / z)
                                guard u >= 0, u < f.width, v >= 0, v < f.height else { continue }
                                let di = v * f.width + u
                                let d = f.depth[di]
                                guard d > 0 else { continue }
                                let c = f.confidence?[di] ?? 2
                                guard c >= 1 else { continue }
                                let sdf = d - z
                                guard sdf > -trunc else { continue }
                                let wt: Float = c >= 2 ? 1 : 0.4
                                let val = min(1, max(-1, sdf / trunc))
                                ts = (ts * ws + val * wt) / (ws + wt)
                                ws += wt
                            }
                            t[idx] = ts
                            w[idx] = ws
                        }
                    }
                }
            }
        }
        return (tsdf, weight)
    }

    /// Outside the box footprint (+ margin) only the table: voxels higher than it by `tableBand` lose their weight.
    static func clearNeighbours(_ weight: inout [Float], grid: Grid, groundY: Float?) {
        guard let groundY else { return }
        let half = grid.box.extent / 2
        let mx = max(footprintMargin * half.x, 0.01), mz = max(footprintMargin * half.z, 0.01)
        for i in 0..<grid.nx {
            let lx = grid.lo.x + (Float(i) + 0.5) * grid.voxel
            for k in 0..<grid.nz {
                let lz = grid.lo.z + (Float(k) + 0.5) * grid.voxel
                guard abs(lx) > half.x + mx || abs(lz) > half.z + mz else { continue }
                for j in 0..<grid.ny {
                    let y = grid.lo.y + (Float(j) + 0.5) * grid.voxel + grid.box.center.y
                    if y > groundY + tableBand { weight[grid.index(i, j, k)] = 0 }
                }
            }
        }
    }

    // MARK: Surface Nets

    /// Vertices (in fractional voxel indices) and triangles of the zero level, outward-facing (tsdf > 0 outside).
    static func surfaceNets(tsdf: [Float], weight: [Float], grid: Grid) -> ([SIMD3<Float>], [UInt32]) {
        let (nx, ny, nz) = (grid.nx, grid.ny, grid.nz)
        func known(_ i: Int, _ j: Int, _ k: Int) -> Bool { weight[grid.index(i, j, k)] >= minWeight }
        func val(_ i: Int, _ j: Int, _ k: Int) -> Float { tsdf[grid.index(i, j, k)] }
        // cell (i, j, k) spans voxels i...i+1 etc.
        let cx = nx - 1, cy = ny - 1, cz = nz - 1
        guard cx > 0, cy > 0, cz > 0 else { return ([], []) }
        var vid = [Int32](repeating: -1, count: cx * cy * cz)
        @inline(__always) func cell(_ i: Int, _ j: Int, _ k: Int) -> Int { (i * cy + j) * cz + k }
        var verts: [SIMD3<Float>] = []
        let corners: [SIMD3<Int>] = [SIMD3(0, 0, 0), SIMD3(1, 0, 0), SIMD3(0, 1, 0), SIMD3(1, 1, 0),
                                     SIMD3(0, 0, 1), SIMD3(1, 0, 1), SIMD3(0, 1, 1), SIMD3(1, 1, 1)]
        let edges: [(Int, Int)] = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
        for i in 0..<cx {
            for j in 0..<cy {
                for k in 0..<cz {
                    var vals = [Float](repeating: 0, count: 8)
                    var ok = true, nin = 0
                    for (c, o) in corners.enumerated() {
                        let a = i + o.x, b = j + o.y, d = k + o.z
                        if !known(a, b, d) { ok = false; break }
                        vals[c] = val(a, b, d)
                        if vals[c] < 0 { nin += 1 }
                    }
                    guard ok, nin > 0, nin < 8 else { continue }
                    var acc = SIMD3<Float>(), cnt: Float = 0
                    for (a, b) in edges where (vals[a] < 0) != (vals[b] < 0) {
                        let t = vals[a] / (vals[a] - vals[b])
                        let pa = SIMD3<Float>(Float(corners[a].x), Float(corners[a].y), Float(corners[a].z))
                        let pb = SIMD3<Float>(Float(corners[b].x), Float(corners[b].y), Float(corners[b].z))
                        acc += pa + t * (pb - pa)
                        cnt += 1
                    }
                    vid[cell(i, j, k)] = Int32(verts.count)
                    verts.append(SIMD3(Float(i), Float(j), Float(k)) + acc / cnt)
                }
            }
        }
        var tris: [UInt32] = []
        // One quad per sign-changing grid edge along each axis, between the 4 cells sharing it.
        for axis in 0..<3 {
            let (o1, o2) = axis == 0 ? (1, 2) : axis == 1 ? (2, 0) : (0, 1)
            let dims = [nx, ny, nz]
            for i in 0..<(axis == 0 ? nx - 1 : nx) {
                for j in 0..<(axis == 1 ? ny - 1 : ny) {
                    for k in 0..<(axis == 2 ? nz - 1 : nz) {
                        let e = [i, j, k]
                        guard e[o1] >= 1, e[o2] >= 1, e[o1] < dims[o1] - 1, e[o2] < dims[o2] - 1 else { continue }
                        var f = e
                        f[axis] += 1
                        guard known(i, j, k), known(f[0], f[1], f[2]) else { continue }
                        let ia = val(i, j, k) < 0, ib = val(f[0], f[1], f[2]) < 0
                        guard ia != ib else { continue }
                        var q: [Int32] = []
                        for (d1, d2) in [(0, 0), (1, 0), (1, 1), (0, 1)] {
                            var c = e
                            c[o1] -= d1
                            c[o2] -= d2
                            c[axis] = min(c[axis], [cx, cy, cz][axis] - 1)
                            q.append(vid[cell(c[0], c[1], c[2])])
                        }
                        guard q.allSatisfy({ $0 >= 0 }) else { continue }
                        // inside -> outside along +axis keeps the cycle (o1, o2) right-handed about +axis
                        if !ia { q.reverse() }
                        let u = q.map { UInt32($0) }
                        tris += [u[0], u[1], u[2], u[0], u[2], u[3]]
                    }
                }
            }
        }
        return (verts, tris)
    }

    // MARK: Cleaning

    /// The connected piece (triangles sharing vertices) with the most triangles near the box's centre line.
    static func keepCentral(_ v: [SIMD3<Float>], _ t: [UInt32], box: ShellBox) -> ([SIMD3<Float>], [UInt32]) {
        var parent = Array(0..<v.count)
        func root(_ x: Int) -> Int {
            var x = x
            while parent[x] != x { parent[x] = parent[parent[x]]; x = parent[x] }
            return x
        }
        for f in Swift.stride(from: 0, to: t.count, by: 3) {
            let a = root(Int(t[f])), b = root(Int(t[f + 1])), c = root(Int(t[f + 2]))
            parent[b] = a
            parent[root(c)] = a
        }
        var score: [Int: Int] = [:]
        let half = SIMD2(box.extent.x, box.extent.z) / 2
        for f in Swift.stride(from: 0, to: t.count, by: 3) {
            let c = (v[Int(t[f])] + v[Int(t[f + 1])] + v[Int(t[f + 2])]) / 3
            let l = box.local(c)
            let r = simd_length(SIMD2(l.x, l.z) / max(half, SIMD2(repeating: 0.01)))
            if r < 0.5 { score[root(Int(t[f])), default: 0] += 1 }
        }
        guard let best = score.max(by: { $0.value < $1.value })?.key else { return (v, t) }
        var remap = [Int32](repeating: -1, count: v.count)
        var nv: [SIMD3<Float>] = [], nt: [UInt32] = []
        for f in Swift.stride(from: 0, to: t.count, by: 3) where root(Int(t[f])) == best {
            for k in 0..<3 {
                let o = Int(t[f + k])
                if remap[o] < 0 { remap[o] = Int32(nv.count); nv.append(v[o]) }
                nt.append(UInt32(remap[o]))
            }
        }
        return (nv, nt)
    }

    /// Taubin λ|μ smoothing over the vertex graph (smooths LiDAR noise without shrinking the object).
    static func taubin(_ v: inout [SIMD3<Float>], _ t: [UInt32], iterations: Int, lambda: Float = 0.5, mu: Float = -0.53) {
        var nbr = [[Int32]](repeating: [], count: v.count)
        for f in Swift.stride(from: 0, to: t.count, by: 3) {
            let a = Int32(t[f]), b = Int32(t[f + 1]), c = Int32(t[f + 2])
            nbr[Int(a)] += [b, c]; nbr[Int(b)] += [a, c]; nbr[Int(c)] += [a, b]
        }
        for i in 0..<nbr.count { nbr[i] = Array(Set(nbr[i])) }
        var tmp = v
        for it in 0..<(2 * iterations) {
            let s = it % 2 == 0 ? lambda : mu
            for i in 0..<v.count where !nbr[i].isEmpty {
                var m = SIMD3<Float>()
                for j in nbr[i] { m += v[Int(j)] }
                m /= Float(nbr[i].count)
                tmp[i] = v[i] + s * (m - v[i])
            }
            swap(&v, &tmp)
        }
    }

    // MARK: All

    static func build(frames: [DepthFrameData], box: ShellBox, smoothIterations: Int = 10) -> Result? {
        var s: [String: Double] = [:]
        var t0 = Date()
        let ground = groundHeight(frames, box: box)
        let g = grid(box: box, groundY: ground)
        s["ground_s"] = Date().timeIntervalSince(t0); t0 = Date()
        var (tsdf, weight) = fuse(frames, grid: g)
        clearNeighbours(&weight, grid: g, groundY: ground)
        s["fuse_s"] = Date().timeIntervalSince(t0); t0 = Date()
        let (vi, tris) = surfaceNets(tsdf: tsdf, weight: weight, grid: g)
        guard !tris.isEmpty else { return nil }
        var v = vi.map { g.world($0.x, $0.y, $0.z) }
        var t = tris
        (v, t) = keepCentral(v, t, box: box)
        taubin(&v, t, iterations: smoothIterations)
        s["extract_s"] = Date().timeIntervalSince(t0)
        return Result(positions: v, indices: t, groundY: ground, voxel: g.voxel, seconds: s)
    }
}
