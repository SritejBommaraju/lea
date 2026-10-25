import Foundation
import simd

/// The object as the tracker knows it, in the object frame: surface samples averaged per 4 mm voxel with normals,
/// plus a 12 mm level of the same for wide (coarse) correspondences. Grows while tracking is good (new surfaces,
/// such as the underside after a flip); a voxel's position is the running mean of its observations.
///
/// Lookups use a flat open-addressing hash (no Dictionary), so ~1500 nearest-neighbour queries × 12 ICP passes stay
/// within a few milliseconds.
struct ObjectModel: Sendable {
    struct Level: Sendable {
        let cell: Float
        private(set) var keys: [Int64]
        private(set) var slots: [Int32]
        private(set) var sum: [SIMD3<Float>] = []
        private(set) var normalSum: [SIMD3<Float>] = []
        private(set) var count: [Float] = []
        private(set) var position: [SIMD3<Float>] = []
        private(set) var normal: [SIMD3<Float>] = []
        private let mask: Int

        init(cell: Float, capacityLog2: Int) {
            self.cell = cell
            keys = [Int64](repeating: .min, count: 1 << capacityLog2)
            slots = [Int32](repeating: -1, count: 1 << capacityLog2)
            mask = (1 << capacityLog2) - 1
        }

        var size: Int { position.count }
        var full: Bool { position.count * 2 >= keys.count }

        @inline(__always) func cellOf(_ p: SIMD3<Float>) -> SIMD3<Int32> {
            SIMD3(Int32((p.x / cell).rounded(.down)), Int32((p.y / cell).rounded(.down)), Int32((p.z / cell).rounded(.down)))
        }

        @inline(__always) static func pack(_ c: SIMD3<Int32>) -> Int64 {
            (Int64(c.x & 0x1FFFFF) << 42) | (Int64(c.y & 0x1FFFFF) << 21) | Int64(c.z & 0x1FFFFF)
        }

        @inline(__always) private func hash(_ k: Int64) -> Int {
            Int(truncatingIfNeeded: (UInt64(bitPattern: k) &* 0x9E37_79B9_7F4A_7C15) >> 20) & mask
        }

        @inline(__always) func find(_ c: SIMD3<Int32>) -> Int? {
            let k = Self.pack(c)
            var s = hash(k)
            while true {
                let e = keys[s]
                if e == .min { return nil }
                if e == k { return Int(slots[s]) }
                s = (s + 1) & mask
            }
        }

        /// Adds an observation; returns false when the level is full.
        @discardableResult
        mutating func add(_ p: SIMD3<Float>, normal n: SIMD3<Float>?, weight: Float = 1) -> Bool {
            let k = Self.pack(cellOf(p))
            var s = hash(k)
            while true {
                let e = keys[s]
                if e == k {
                    let i = Int(slots[s])
                    // Cap the weight so the model keeps adapting (a voxel is never frozen by its first views).
                    let w = count[i] >= 30 ? weight * count[i] / 30 : weight
                    sum[i] += p * w
                    count[i] += w
                    position[i] = sum[i] / count[i]
                    if let n {
                        normalSum[i] += simd_dot(n, normalSum[i]) < 0 ? -n * w : n * w
                        let l = simd_length(normalSum[i])
                        if l > 1e-6 { normal[i] = normalSum[i] / l }
                    }
                    return true
                }
                if e == .min {
                    guard !full else { return false }
                    keys[s] = k
                    slots[s] = Int32(position.count)
                    sum.append(p * weight)
                    count.append(weight)
                    position.append(p)
                    normalSum.append(n.map { $0 * weight } ?? .zero)
                    normal.append(n ?? .zero)
                    return true
                }
                s = (s + 1) & mask
            }
        }

        mutating func setNormal(_ i: Int, _ n: SIMD3<Float>) {
            normal[i] = n
            normalSum[i] = n * count[i]
        }

        /// The nearest stored point to `q` within `maxDist` (searches the 27 cells around q; maxDist ≤ cell gives an
        /// exact answer).
        @inline(__always)
        func nearest(_ q: SIMD3<Float>, maxDist: Float) -> (index: Int, d2: Float)? {
            let c = cellOf(q)
            var best = -1
            var bestD2 = maxDist * maxDist
            for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 {
                if let i = find(c &+ SIMD3(dx, dy, dz)) {
                    let d2 = simd_distance_squared(position[i], q)
                    if d2 < bestD2 { bestD2 = d2; best = i }
                }
            } } }
            return best >= 0 ? (best, bestD2) : nil
        }

        /// Whether any stored point lies within one cell of `q` (a cheap "is this surface known" test).
        func occupiedNear(_ q: SIMD3<Float>) -> Bool {
            let c = cellOf(q)
            for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 where find(c &+ SIMD3(dx, dy, dz)) != nil {
                return true
            } } }
            return false
        }

        /// Neighbours of `q` within `radius` (≤ cell).
        func neighbours(_ q: SIMD3<Float>, radius: Float) -> [SIMD3<Float>] {
            let c = cellOf(q)
            var out: [SIMD3<Float>] = []
            for dx in Int32(-1)...1 { for dy in Int32(-1)...1 { for dz in Int32(-1)...1 {
                if let i = find(c &+ SIMD3(dx, dy, dz)), simd_distance(position[i], q) <= radius { out.append(position[i]) }
            } } }
            return out
        }
    }

    static let fineCell: Float = 0.004
    static let coarseCell: Float = 0.012

    private(set) var fine = Level(cell: fineCell, capacityLog2: 17)
    private(set) var coarse = Level(cell: coarseCell, capacityLog2: 15)
    /// The locked box in the object frame (centre, half size, yaw): new points outside it are not added.
    let box: TrackBox

    init(box: TrackBox) { self.box = box }

    var count: Int { fine.size }
    var centroid: SIMD3<Float> {
        guard fine.size > 0 else { return box.center }
        return fine.position.reduce(.zero, +) / Float(fine.size)
    }

    /// Radius around the centroid that holds 95 % of the model (for pose-distance probes and the lost search).
    var radius: Float {
        guard fine.size > 10 else { return max(0.02, simd_length(box.half)) }
        let c = centroid
        let d = fine.position.map { simd_distance($0, c) }.sorted()
        return max(0.02, d[Int(Double(d.count - 1) * 0.95)])
    }

    mutating func add(_ p: SIMD3<Float>, normal: SIMD3<Float>?, weight: Float = 1) {
        guard box.contains(p, margin: 0.004) else { return }
        fine.add(p, normal: normal, weight: weight)
        coarse.add(p, normal: normal, weight: weight)
    }

    /// Seeds from points without normals (the scan's cloud at lock): normals by PCA of the 4 mm neighbourhood,
    /// pointing away from the model's centroid (fine for the everyday convex-ish objects a hand turns).
    mutating func seed(_ pts: [SIMD3<Float>]) {
        for p in pts { add(p, normal: nil) }
        estimateMissingNormals()
    }

    mutating func estimateMissingNormals() {
        let c = centroid
        for i in 0..<fine.size where simd_length_squared(fine.normal[i]) < 0.5 {
            let q = fine.position[i]
            // The 12 mm level's neighbourhood (~2 cm): LiDAR noise makes 4 mm neighbourhoods' normals unreliable.
            let nb = coarse.neighbours(q, radius: 0.02)
            var n = Self.pcaNormal(nb) ?? simd_normalize(q - c + SIMD3(0, 1e-6, 0))
            if simd_dot(n, q - c) < 0 { n = -n }
            fine.setNormal(i, n)
        }
        for i in 0..<coarse.size where simd_length_squared(coarse.normal[i]) < 0.5 {
            let q = coarse.position[i]
            let nb = coarse.neighbours(q, radius: 0.022)
            var n = Self.pcaNormal(nb) ?? simd_normalize(q - c + SIMD3(0, 1e-6, 0))
            if simd_dot(n, q - c) < 0 { n = -n }
            coarse.setNormal(i, n)
        }
    }

    static func pcaNormal(_ nb: [SIMD3<Float>]) -> SIMD3<Float>? {
        guard nb.count >= 4 else { return nil }
        let m = nb.reduce(.zero, +) / Float(nb.count)
        var c = [Double](repeating: 0, count: 9)
        for v in nb {
            let d = SIMD3<Double>(v - m)
            for r in 0..<3 { for k in 0..<3 { c[r * 3 + k] += d[r] * d[k] } }
        }
        let (values, vectors) = DenseSolve.symmetricEigen(c, n: 3)
        guard let i = (0..<3).min(by: { values[$0] < values[$1] }) else { return nil }
        let n = SIMD3<Float>(Float(vectors[0 * 3 + i]), Float(vectors[1 * 3 + i]), Float(vectors[2 * 3 + i]))
        return simd_length(n) > 1e-6 ? simd_normalize(n) : nil
    }
}
