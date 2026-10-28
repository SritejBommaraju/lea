import Foundation
import simd

/// The locked object: an upright box on the support plane in ARKit world metres (Y up), turned by `yaw` about Y.
struct ObjectBox: Sendable, Equatable {
    var center: SIMD3<Float>
    /// Width (local x), height (y), depth (local z), metres.
    var size: SIMD3<Float>
    var yaw: Float

    var planeY: Float { center.y - size.y / 2 }
    var diagonal: Float { simd_length(size) }

    /// World-space corners (8).
    var corners: [SIMD3<Float>] {
        let h = size / 2
        let r = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
        return [-1, 1].flatMap { x in [-1, 1].flatMap { y in [-1, 1].map { z in
            center + r.act(SIMD3(Float(x) * h.x, Float(y) * h.y, Float(z) * h.z))
        } } }
    }

    func contains(_ p: SIMD3<Float>, margin: Float = 0) -> Bool {
        let local = simd_quatf(angle: -yaw, axis: SIMD3(0, 1, 0)).act(p - center)
        let h = size / 2 + margin
        return abs(local.x) <= h.x && abs(local.y) <= h.y && abs(local.z) <= h.z
    }

    func scaled(_ s: Float) -> ObjectBox {
        var b = self
        b.size = simd_max(size * s, SIMD3(repeating: BoxFitter.minSide))
        b.center.y = planeY + b.size.y / 2
        return b
    }

    var contract: CropBox {
        let q = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
        return CropBox(centerM: [Double(center.x), Double(center.y), Double(center.z)],
                       extentM: [Double(size.x), Double(size.y), Double(size.z)], frame: .arkitWorld,
                       quaternionXyzw: [Double(q.imag.x), Double(q.imag.y), Double(q.imag.z), Double(q.real)])
    }
}

/// Tap-to-lock: from LiDAR points near the tap, keep the connected cluster the tap ray points at and fit a tight
/// upright box above the support plane.
enum BoxFitter {
    static let searchRadius: Float = 0.25
    static let minAbovePlane: Float = 0.005
    static let maxAbovePlane: Float = 0.40
    static let cell: Float = 0.01
    static let padding: Float = 1.15
    static let minSide: Float = 0.04
    static let minClusterCells = 3

    /// Low objects (a board, a thin part) sit inside the support plane's noise: their footprint comes from colour.
    static let lowObject: Float = 0.02
    /// Minimum footprint (x and z): a small object still gets a box about 8 cm across.
    static let minFootprint: Float = 0.08
    static let colorStep: Float = 45     // RGB distance from the support's colour

    /// - Parameters:
    ///   - points: world points (confidence ≥ medium) from the accumulated cloud; `colors` matches them when known.
    ///   - tap: where the tap ray hit the support plane (or the object).
    ///   - rayOrigin/rayDirection: the tap ray, to pick the cluster the person pointed at.
    ///   - refinePlane: the plane came from ARKit's estimate, not a detected plane anchor. Its height can be off by a
    ///     centimetre or two, which lets the floor itself join the object's cluster (a box far too big). The plane is
    ///     then moved to the measured support layer under the tap.
    /// Points must clear the plane by twice its measured noise (2–10 mm), not a fixed margin, so low objects keep
    /// their height; objects lower than 2 cm also grow their footprint over the region whose colour differs from
    /// the support around them.
    static func fit(points: [SIMD3<Float>], colors: [SIMD3<UInt8>]? = nil, tap: SIMD3<Float>, planeY estimated: Float,
                    rayOrigin: SIMD3<Float>, rayDirection: SIMD3<Float>, refinePlane: Bool = false) -> ObjectBox? {
        let dir = simd_normalize(rayDirection)
        var planeY = estimated
        var minAbove = minAbovePlane
        if let support = supportPlane(points: points, around: tap, near: estimated, refine: refinePlane) {
            if refinePlane { planeY = support.y }
            minAbove = min(0.01, max(0.002, 2 * support.sigma))
        } else if refinePlane {
            minAbove = 0.01
        }
        var cells: [SIMD3<Int32>: [SIMD3<Float>]] = [:]
        for p in points {
            let above = p.y - planeY
            guard above >= minAbove, above <= maxAbovePlane else { continue }
            let dx = p.x - tap.x, dz = p.z - tap.z
            guard dx * dx + dz * dz <= searchRadius * searchRadius else { continue }
            cells[key(p), default: []].append(p)
        }

        var cluster: [SIMD3<Float>] = []
        if !cells.isEmpty {
            // Seed: the occupied cell closest to the tap ray (and in front of the camera).
            func rayDistance(_ k: SIMD3<Int32>) -> Float {
                let c = (SIMD3<Float>(Float(k.x), Float(k.y), Float(k.z)) + 0.5) * cell
                let v = c - rayOrigin
                let t = max(0, simd_dot(v, dir))
                return simd_length(v - t * dir)
            }
            let seed = cells.keys.min { rayDistance($0) < rayDistance($1) }!
            // 26-connected flood fill on the 1 cm grid.
            var seen: Set<SIMD3<Int32>> = [seed]
            var stack = [seed]
            while let k = stack.popLast() {
                for dx: Int32 in -1...1 { for dy: Int32 in -1...1 { for dz: Int32 in -1...1 {
                    let n = k &+ SIMD3(dx, dy, dz)
                    if cells[n] != nil, !seen.contains(n) {
                        seen.insert(n)
                        stack.append(n)
                    }
                } } }
            }
            if seen.count >= minClusterCells { cluster = seen.flatMap { cells[$0]! } }
        }
        let top = cluster.map(\.y).max().map { $0 - planeY } ?? 0
        if top < lowObject, let colors, colors.count == points.count {
            let region = colorRegion(points: points, colors: colors, tap: tap, planeY: planeY, band: minAbove + 0.004,
                                     seeds: cluster)
            cluster += region
        }
        guard !cluster.isEmpty else { return nil }
        return box(around: cluster, planeY: planeY)
    }

    /// A phone without LiDAR has no depth cloud, so `fit` finds nothing. ARKit's sparse feature points still land on
    /// textured objects: the ones within `sparseRadius` of the tap and above the support are the object's footprint.
    /// With too few of them (a plain, glossy or dark object) the box is a default one centred on the tap, and the
    /// result says so (`measured` false): the person can tap the object to move it, and the session notes that the
    /// size was assumed.
    static let sparseRadius: Float = 0.20
    static let sparseMinPoints = 6
    /// The default footprint and height (m) of an object ARKit gave no points for: about a hand-held gadget.
    static let assumedSize = SIMD3<Float>(0.15, 0.10, 0.15)

    struct SparseFit: Equatable, Sendable {
        var box: ObjectBox
        /// The box came from feature points (true) or is the default (false).
        var measured: Bool
        var pointsUsed: Int
    }

    static func fitSparse(featurePoints: [SIMD3<Float>], tap: SIMD3<Float>, planeY: Float) -> SparseFit {
        let near = featurePoints.filter { p in
            let above = p.y - planeY
            let dx = p.x - tap.x, dz = p.z - tap.z
            return above >= 0.01 && above <= maxAbovePlane && dx * dx + dz * dz <= sparseRadius * sparseRadius
        }
        if near.count >= sparseMinPoints {
            // Drop strays: a feature point on the wall behind or a cable beside it. Keep what lies within twice the
            // median distance from the tap (at least 6 cm).
            let distances = near.map { hypot($0.x - tap.x, $0.z - tap.z) }.sorted()
            let keep = max(0.06, 2 * distances[distances.count / 2])
            let core = near.filter { hypot($0.x - tap.x, $0.z - tap.z) <= keep }
            if core.count >= sparseMinPoints, let fitted = box(around: core, planeY: planeY) {
                return SparseFit(box: fitted, measured: true, pointsUsed: core.count)
            }
        }
        let size = assumedSize
        let fallback = ObjectBox(center: SIMD3(tap.x, planeY + size.y / 2, tap.z), size: size, yaw: 0)
        return SparseFit(box: fallback, measured: false, pointsUsed: near.count)
    }

    /// The support under the tap: the lowest dense layer within ±15 cm of the estimate (5 mm bins, "dense" = at least
    /// half the busiest bin), measured on a ring 8–35 cm from the tap so the object itself does not count. Returns
    /// the layer's mean height and its noise (σ).
    static func supportPlane(points: [SIMD3<Float>], around tap: SIMD3<Float>, near y0: Float,
                             refine: Bool) -> (y: Float, sigma: Float)? {
        let bin: Float = 0.005, span: Float = refine ? 0.15 : 0.03
        var counts = [Int](repeating: 0, count: Int(2 * span / bin))
        var ring: [Float] = []
        for p in points {
            let dx = p.x - tap.x, dz = p.z - tap.z
            let r2 = dx * dx + dz * dz
            guard r2 <= 0.35 * 0.35, r2 >= 0.08 * 0.08 else { continue }
            let i = Int(((p.y - y0) + span) / bin)
            if i >= 0, i < counts.count { counts[i] += 1; ring.append(p.y) }
        }
        guard let most = counts.max(), most >= 50, let i = counts.firstIndex(where: { $0 * 2 >= most }) else { return nil }
        let center = y0 - span + (Float(i) + 0.5) * bin
        let layer = ring.filter { abs($0 - center) <= 0.012 }
        guard layer.count >= 30 else { return nil }
        let mean = layer.reduce(0, +) / Float(layer.count)
        let sigma = (layer.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Float(layer.count)).squareRoot()
        return (mean, sigma)
    }

    /// Kept for callers that only need the support's height.
    static func supportLayer(points: [SIMD3<Float>], around tap: SIMD3<Float>, near y0: Float) -> Float? {
        supportPlane(points: points, around: tap, near: y0, refine: true)?.y
    }

    /// Near-plane points (within `band`) on a 1 cm grid whose colour differs from the support's (the median colour
    /// 15–25 cm from the tap), grown 8-connected from the cluster's footprint and the tap: a low object's footprint.
    static func colorRegion(points: [SIMD3<Float>], colors: [SIMD3<UInt8>], tap: SIMD3<Float>, planeY: Float,
                            band: Float, seeds: [SIMD3<Float>]) -> [SIMD3<Float>] {
        var cellColor: [SIMD2<Int32>: (sum: SIMD3<Float>, n: Float, pts: [SIMD3<Float>])] = [:]
        var supportColors: [SIMD3<Float>] = []
        for (p, c) in zip(points, colors) {
            guard abs(p.y - planeY) <= band else { continue }
            let dx = p.x - tap.x, dz = p.z - tap.z
            let r2 = dx * dx + dz * dz
            let rgb = SIMD3<Float>(c)
            if r2 >= 0.15 * 0.15, r2 <= 0.25 * 0.25 { supportColors.append(rgb) }
            guard r2 <= searchRadius * searchRadius else { continue }
            let k = SIMD2<Int32>(Int32((p.x / cell).rounded(.down)), Int32((p.z / cell).rounded(.down)))
            cellColor[k, default: (.zero, 0, [])].sum += rgb
            cellColor[k, default: (.zero, 0, [])].n += 1
            cellColor[k, default: (.zero, 0, [])].pts.append(p)
        }
        guard supportColors.count >= 20 else { return [] }
        func median(_ v: [Float]) -> Float { v.sorted()[v.count / 2] }
        let support = SIMD3(median(supportColors.map(\.x)), median(supportColors.map(\.y)), median(supportColors.map(\.z)))
        func distinct(_ k: SIMD2<Int32>) -> Bool {
            guard let c = cellColor[k] else { return false }
            return simd_length(c.sum / c.n - support) > colorStep
        }
        let tapKey = SIMD2<Int32>(Int32((tap.x / cell).rounded(.down)), Int32((tap.z / cell).rounded(.down)))
        var stack = ([tapKey] + seeds.map { SIMD2<Int32>(Int32(($0.x / cell).rounded(.down)), Int32(($0.z / cell).rounded(.down))) })
            .filter(distinct)
        var seen = Set(stack)
        while let k = stack.popLast() {
            for dx: Int32 in -1...1 { for dz: Int32 in -1...1 {
                let n = k &+ SIMD2(dx, dz)
                if !seen.contains(n), distinct(n) {
                    seen.insert(n)
                    stack.append(n)
                }
            } }
        }
        return seen.flatMap { cellColor[$0]?.pts ?? [] }
    }

    /// Smallest-footprint upright box (yaw searched in 3° steps), padded, at least `minSide` per side, standing on
    /// the plane.
    static func box(around pts: [SIMD3<Float>], planeY: Float) -> ObjectBox? {
        guard !pts.isEmpty else { return nil }
        var best: (area: Float, yaw: Float, lo: SIMD2<Float>, hi: SIMD2<Float>)?
        for step in 0..<30 {
            let yaw = Float(step) * 3 * .pi / 180
            let c = cos(yaw), s = sin(yaw)
            var lo = SIMD2<Float>(repeating: .greatestFiniteMagnitude), hi = -lo
            for p in pts {
                // Rotate into the box frame (inverse yaw about Y).
                let q = SIMD2(c * p.x - s * p.z, s * p.x + c * p.z)
                lo = simd_min(lo, q)
                hi = simd_max(hi, q)
            }
            let area = (hi.x - lo.x) * (hi.y - lo.y)
            if best == nil || area < best!.area { best = (area, yaw, lo, hi) }
        }
        guard let best else { return nil }
        let top = pts.map(\.y).max()! - planeY
        let mid = (best.lo + best.hi) / 2
        let c = cos(best.yaw), s = sin(best.yaw)
        // Back to world: inverse of the rotation above.
        let centerXZ = SIMD2(c * mid.x + s * mid.y, -s * mid.x + c * mid.y)
        let size = simd_max(SIMD3(best.hi.x - best.lo.x, top, best.hi.y - best.lo.y) * padding,
                            SIMD3(minFootprint, minSide, minFootprint))
        return ObjectBox(center: SIMD3(centerXZ.x, planeY + size.y / 2, centerXZ.y), size: size, yaw: best.yaw)
    }

    static func key(_ p: SIMD3<Float>) -> SIMD3<Int32> {
        SIMD3(Int32((p.x / cell).rounded(.down)), Int32((p.y / cell).rounded(.down)), Int32((p.z / cell).rounded(.down)))
    }
}
