import Foundation
import simd

// Colour-image features for the hand-held tracker, our own implementation of the classic recipe (FAST corners,
// intensity-centroid orientation, steered binary tests: Rosten & Drummond 2006, Rublee et al. 2011). The binary-test
// pattern is our own, drawn from a seeded Gaussian. Features are lifted to 3D with the LiDAR depth so a rigid fit
// (random-sample consensus + Horn) gives the object's pose even where ICP alone slides (a cylinder, a flat face).

/// An 8-bit grey image, row-major.
struct GrayImage: Sendable {
    var width: Int
    var height: Int
    var pixels: [UInt8]

    @inline(__always) subscript(x: Int, y: Int) -> UInt8 { pixels[y * width + x] }

    /// 5 × 5 box blur (separable running sums): the binary tests compare smoothed values, as in BRIEF.
    func blurred() -> GrayImage {
        let w = width, h = height
        var tmp = [UInt16](repeating: 0, count: w * h)
        var out = [UInt8](repeating: 0, count: w * h)
        pixels.withUnsafeBufferPointer { src in
            tmp.withUnsafeMutableBufferPointer { t in
                for y in 0..<h {
                    let r = y * w
                    var s = 0
                    for x in -2...2 { s += Int(src[r + min(w - 1, max(0, x))]) }
                    for x in 0..<w {
                        t[r + x] = UInt16(s)
                        s += Int(src[r + min(w - 1, x + 3)]) - Int(src[r + max(0, x - 2)])
                    }
                }
            }
            tmp.withUnsafeBufferPointer { t in
                out.withUnsafeMutableBufferPointer { o in
                    for x in 0..<w {
                        var s = 0
                        for y in -2...2 { s += Int(t[min(h - 1, max(0, y)) * w + x]) }
                        for y in 0..<h {
                            o[y * w + x] = UInt8(min(255, s / 25))
                            s += Int(t[min(h - 1, y + 3) * w + x]) - Int(t[max(0, y - 2) * w + x])
                        }
                    }
                }
            }
        }
        return GrayImage(width: w, height: h, pixels: out)
    }
}

/// A 256-bit binary descriptor.
struct Descriptor: Sendable, Equatable {
    var a: UInt64 = 0, b: UInt64 = 0, c: UInt64 = 0, d: UInt64 = 0

    @inline(__always) func distance(_ o: Descriptor) -> Int {
        (a ^ o.a).nonzeroBitCount + (b ^ o.b).nonzeroBitCount + (c ^ o.c).nonzeroBitCount + (d ^ o.d).nonzeroBitCount
    }

    @inline(__always) mutating func set(_ bit: Int) {
        switch bit >> 6 {
        case 0: a |= 1 << UInt64(bit & 63)
        case 1: b |= 1 << UInt64(bit & 63)
        case 2: c |= 1 << UInt64(bit & 63)
        default: d |= 1 << UInt64(bit & 63)
        }
    }
}

struct Keypoint: Sendable {
    var x: Float, y: Float
    var angle: Float
    var score: Float
    var descriptor: Descriptor
}

enum FeatureDetector {
    static let threshold = 18
    static let border = 20
    static let orientationRadius = 12
    static let angleBins = 32

    /// The 16-pixel Bresenham circle of radius 3.
    static let circle: [(Int, Int)] = [(0, -3), (1, -3), (2, -2), (3, -1), (3, 0), (3, 1), (2, 2), (1, 3),
                                       (0, 3), (-1, 3), (-2, 2), (-3, 1), (-3, 0), (-3, -1), (-2, -2), (-1, -3)]

    /// 256 point pairs within ±12 px (σ = 5), seeded: our own pattern.
    static let pattern: [(SIMD2<Float>, SIMD2<Float>)] = {
        var rng = SplitMix64(seed: 0x5EED_0B7E_C7)
        func sample() -> SIMD2<Float> {
            while true {
                let p = SIMD2(rng.gaussian() * 5, rng.gaussian() * 5)
                if simd_length(p) <= 12 { return p }
            }
        }
        return (0..<256).map { _ in (sample(), sample()) }
    }()

    /// Pixel offsets of the pattern rotated to each of `angleBins` angles, for an image `width` wide.
    static func steered(width: Int) -> [[(Int32, Int32)]] {
        (0..<angleBins).map { bin in
            let a = Float(bin) / Float(angleBins) * 2 * .pi
            let c = cos(a), s = sin(a)
            return pattern.map { p, q in
                func off(_ v: SIMD2<Float>) -> Int32 {
                    let x = Int((c * v.x - s * v.y).rounded()), y = Int((s * v.x + c * v.y).rounded())
                    return Int32(y * width + x)
                }
                return (off(p), off(q))
            }
        }
    }

    /// FAST-9 corners inside `region` (x0, y0, x1, y1) where `allowed(x, y)` holds, 3 × 3 non-maximum suppressed,
    /// the strongest `maxCount` (spread over a 4 × 4 grid of the region), with orientation and descriptor.
    static func detect(_ img: GrayImage, region: (Int, Int, Int, Int)? = nil, maxCount: Int = 300,
                       allowed: ((Int, Int) -> Bool)? = nil) -> [Keypoint] {
        let w = img.width, h = img.height
        let x0 = max(border, region?.0 ?? 0), y0 = max(border, region?.1 ?? 0)
        let x1 = min(w - border, region?.2 ?? w), y1 = min(h - border, region?.3 ?? h)
        guard x1 > x0 + 2, y1 > y0 + 2 else { return [] }
        let rw = x1 - x0, rh = y1 - y0
        var score = [Float](repeating: 0, count: rw * rh)
        let offs = circle.map { $0.1 * w + $0.0 }
        img.pixels.withUnsafeBufferPointer { p in
            for y in y0..<y1 {
                for x in x0..<x1 {
                    let i = y * w + x
                    let c = Int(p[i])
                    let hi = c + threshold, lo = c - threshold
                    // Quick reject on the four compass pixels: a 9-arc covers at least two of them.
                    let n = Int(p[i + offs[0]]), e = Int(p[i + offs[4]]), s = Int(p[i + offs[8]]), wv = Int(p[i + offs[12]])
                    var bright = 0, dark = 0
                    if n > hi { bright += 1 } else if n < lo { dark += 1 }
                    if e > hi { bright += 1 } else if e < lo { dark += 1 }
                    if s > hi { bright += 1 } else if s < lo { dark += 1 }
                    if wv > hi { bright += 1 } else if wv < lo { dark += 1 }
                    guard bright >= 2 || dark >= 2 else { continue }
                    if let allowed, !allowed(x, y) { continue }
                    var bm: UInt32 = 0, dm: UInt32 = 0
                    var sb = 0, sd = 0
                    for k in 0..<16 {
                        let v = Int(p[i + offs[k]])
                        if v > hi { bm |= 1 << UInt32(k); sb += v - hi } else if v < lo { dm |= 1 << UInt32(k); sd += lo - v }
                    }
                    let arcB = hasArc(bm), arcD = hasArc(dm)
                    guard arcB || arcD else { continue }
                    score[(y - y0) * rw + (x - x0)] = Float(max(arcB ? sb : 0, arcD ? sd : 0))
                }
            }
        }
        // Non-maximum suppression, bucketed so the corners spread over the object.
        var cand: [(x: Int, y: Int, s: Float)] = []
        for y in 1..<(rh - 1) {
            for x in 1..<(rw - 1) {
                let s = score[y * rw + x]
                guard s > 0 else { continue }
                var isMax = true
                for dy in -1...1 { for dx in -1...1 where dx != 0 || dy != 0 {
                    let o = score[(y + dy) * rw + x + dx]
                    if o > s || (o == s && (dy < 0 || (dy == 0 && dx < 0))) { isMax = false }
                } }
                if isMax { cand.append((x + x0, y + y0, s)) }
            }
        }
        let buckets = 4
        var perBucket = [[(x: Int, y: Int, s: Float)]](repeating: [], count: buckets * buckets)
        for c in cand {
            let bx = min(buckets - 1, (c.x - x0) * buckets / rw), by = min(buckets - 1, (c.y - y0) * buckets / rh)
            perBucket[by * buckets + bx].append(c)
        }
        let quota = max(4, maxCount / (buckets * buckets))
        var chosen: [(x: Int, y: Int, s: Float)] = []
        var rest: [(x: Int, y: Int, s: Float)] = []
        for var b in perBucket {
            b.sort { $0.s > $1.s }
            chosen += b.prefix(quota)
            rest += b.dropFirst(quota)
        }
        if chosen.count < maxCount { chosen += rest.sorted { $0.s > $1.s }.prefix(maxCount - chosen.count) }
        if chosen.count > maxCount { chosen = Array(chosen.sorted { $0.s > $1.s }.prefix(maxCount)) }
        let smooth = img.blurred()
        let steer = steered(width: w)
        return chosen.map { c in
            let a = orientation(img, x: c.x, y: c.y)
            return Keypoint(x: Float(c.x), y: Float(c.y), angle: a, score: c.s,
                            descriptor: describe(smooth, x: c.x, y: c.y, angle: a, steer: steer))
        }
    }

    @inline(__always) static func hasArc(_ m: UInt32) -> Bool {
        guard m.nonzeroBitCount >= 9 else { return false }
        var r = m | (m << 16)
        for _ in 0..<8 { r &= (r >> 1) }
        return r & 0xFFFF_FFFF != 0
    }

    /// Intensity-centroid angle over a disc of `orientationRadius`.
    static func orientation(_ img: GrayImage, x: Int, y: Int) -> Float {
        let r = orientationRadius
        var m01 = 0, m10 = 0
        for dy in -r...r {
            let span = Int(Double(r * r - dy * dy).squareRoot())
            let row = (y + dy) * img.width
            for dx in -span...span {
                let v = Int(img.pixels[row + x + dx])
                m10 += dx * v
                m01 += dy * v
            }
        }
        return atan2(Float(m01), Float(m10))
    }

    static func describe(_ smooth: GrayImage, x: Int, y: Int, angle: Float, steer: [[(Int32, Int32)]]) -> Descriptor {
        var a = angle
        if a < 0 { a += 2 * .pi }
        let bin = Int((a / (2 * .pi) * Float(angleBins)).rounded()) % angleBins
        let base = y * smooth.width + x
        var d = Descriptor()
        smooth.pixels.withUnsafeBufferPointer { p in
            for (k, pair) in steer[bin].enumerated() where p[base + Int(pair.0)] < p[base + Int(pair.1)] { d.set(k) }
        }
        return d
    }
}

/// The object's remembered features: object-frame 3D position, descriptor, and the direction it was seen from.
struct FeatureBank: Sendable {
    static let capacity = 3000
    static let cell: Float = 0.02
    private(set) var position: [SIMD3<Float>] = []
    private(set) var descriptor: [Descriptor] = []
    private(set) var seenFrom: [SIMD3<Float>] = []
    private(set) var hits: [Int] = []
    private(set) var born: [Int] = []
    private var grid: [SIMD3<Int32>: [Int32]] = [:]
    private var tick = 0

    var count: Int { position.count }

    @inline(__always) static func key(_ p: SIMD3<Float>) -> SIMD3<Int32> {
        SIMD3(Int32((p.x / cell).rounded(.down)), Int32((p.y / cell).rounded(.down)), Int32((p.z / cell).rounded(.down)))
    }

    func near(_ p: SIMD3<Float>, radius: Float, _ body: (Int) -> Void) {
        let reach = Int32((radius / Self.cell).rounded(.up))
        let k = Self.key(p)
        let r2 = radius * radius
        for dx in -reach...reach { for dy in -reach...reach { for dz in -reach...reach {
            for i in grid[k &+ SIMD3(dx, dy, dz)] ?? [] where simd_distance_squared(position[Int(i)], p) <= r2 { body(Int(i)) }
        } } }
    }

    /// Adds a feature unless the same one (within 4 mm, descriptor within 48 bits) is already known.
    mutating func add(_ p: SIMD3<Float>, _ d: Descriptor, seenFrom v: SIMD3<Float>) {
        var dup = -1
        near(p, radius: 0.004) { i in if dup < 0, descriptor[i].distance(d) <= 48 { dup = i } }
        if dup >= 0 { hits[dup] += 1; return }
        grid[Self.key(p), default: []].append(Int32(position.count))
        position.append(p)
        descriptor.append(d)
        seenFrom.append(v)
        hits.append(1)
        born.append(tick)
        if position.count > Self.capacity { prune() }
    }

    mutating func markMatched(_ i: Int) { hits[i] += 1 }
    mutating func nextFrame() { tick += 1 }

    /// Drops the least useful fifth (rarely matched, oldest first).
    private mutating func prune() {
        let order = position.indices.sorted { (hits[$0], born[$0]) > (hits[$1], born[$1]) }
        let keep = order.prefix(Self.capacity * 4 / 5).sorted()
        position = keep.map { position[$0] }
        descriptor = keep.map { descriptor[$0] }
        seenFrom = keep.map { seenFrom[$0] }
        hits = keep.map { hits[$0] }
        born = keep.map { born[$0] }
        grid = [:]
        for (i, p) in position.enumerated() { grid[Self.key(p), default: []].append(Int32(i)) }
    }
}

/// One 3D–3D correspondence: a feature seen now (world) and a remembered one (object frame).
struct FeatureMatch: Sendable {
    var world: SIMD3<Float>
    var object: SIMD3<Float>
    var bank: Int
    var depth: Float
    var distance: Int = 0
}

enum FeatureMatcher {
    /// Matches seen features to the bank. With `predicted` (world → object), only bank features within `radius` of
    /// where the prediction puts each one, and seen from a similar side, are compared; without it (relocalising),
    /// the whole bank with a stricter ratio test.
    static func match(_ seen: [(world: SIMD3<Float>, desc: Descriptor, depth: Float)], bank: FeatureBank,
                      worldToObject predicted: simd_float4x4?, cameraWorld: SIMD3<Float>, radius: Float) -> [FeatureMatch] {
        var out: [FeatureMatch] = []
        guard bank.count > 0 else { return out }
        for f in seen {
            var best = Int.max, second = Int.max, bi = -1
            if let predicted {
                let q = Rigid.apply(predicted, f.world)
                let view = simd_normalize(Rigid.apply(predicted, cameraWorld) - q)
                bank.near(q, radius: radius) { i in
                    guard simd_dot(bank.seenFrom[i], view) > 0.1 else { return }
                    let d = bank.descriptor[i].distance(f.desc)
                    if d < best { second = best; best = d; bi = i } else if d < second { second = d }
                }
                guard bi >= 0, best <= 64, second == .max || Float(best) <= 0.9 * Float(second) else { continue }
            } else {
                for i in 0..<bank.count {
                    let d = bank.descriptor[i].distance(f.desc)
                    if d < best { second = best; best = d; bi = i } else if d < second { second = d }
                }
                guard bi >= 0, best <= 56, second == .max || Float(best) <= 0.8 * Float(second) else { continue }
            }
            out.append(FeatureMatch(world: f.world, object: bank.position[bi], bank: bi, depth: f.depth, distance: best))
        }
        // One-to-one: a remembered feature keeps only its best match (many-to-one pairs fake a consensus).
        var bestFor: [Int: Int] = [:]
        for (k, m) in out.enumerated() where bestFor[m.bank].map({ out[$0].distance > m.distance }) ?? true { bestFor[m.bank] = k }
        return bestFor.values.sorted().map { out[$0] }
    }

    /// Random-sample consensus on 3-point rigid fits (pairs pre-checked for preserved distances), refit on the inliers with Horn's
    /// method. Returns object → world and the inlier indices, nil under `minInliers`.
    static func consensusFit(_ m: [FeatureMatch], iterations: Int = 160, minInliers: Int = 6, seed: UInt64 = 1)
        -> (objectToWorld: simd_float4x4, inliers: [Int])? {
        guard m.count >= max(3, minInliers) else { return nil }
        var rng = SplitMix64(seed: seed &+ UInt64(m.count))
        func tol(_ i: Int) -> Float { 0.006 + 0.01 * m[i].depth * m[i].depth }
        var bestInl: [Int] = []
        var bestPose: simd_float4x4?
        for _ in 0..<iterations {
            let i = Int.random(in: 0..<m.count, using: &rng)
            let j = Int.random(in: 0..<m.count, using: &rng)
            let k = Int.random(in: 0..<m.count, using: &rng)
            guard i != j, j != k, i != k else { continue }
            // Rigid motions keep distances: reject inconsistent triples before fitting.
            let pairs = [(i, j), (j, k), (i, k)]
            var ok = true
            for (a, b) in pairs {
                let dw = simd_distance(m[a].world, m[b].world), dobj = simd_distance(m[a].object, m[b].object)
                if abs(dw - dobj) > 0.012 || dw < 0.01 { ok = false; break }
            }
            guard ok else { continue }
            // Not collinear.
            let area = simd_length(simd_cross(m[j].object - m[i].object, m[k].object - m[i].object))
            guard area > 2e-4 else { continue }
            guard let t = DenseSolve.absoluteOrientation([m[i].object, m[j].object, m[k].object],
                                                         [m[i].world, m[j].world, m[k].world]) else { continue }
            var inl: [Int] = []
            for (n, c) in m.enumerated() where simd_distance(Rigid.apply(t, c.object), c.world) < tol(n) { inl.append(n) }
            if inl.count > bestInl.count {
                bestInl = inl
                bestPose = t
                if Float(inl.count) > 0.8 * Float(m.count) { break }
            }
        }
        guard bestInl.count >= minInliers, var pose = bestPose else { return nil }
        // Refit on the inliers twice (the set can grow once the pose is better).
        for _ in 0..<2 {
            guard let t = DenseSolve.absoluteOrientation(bestInl.map { m[$0].object }, bestInl.map { m[$0].world }) else { break }
            pose = t
            let inl = m.indices.filter { simd_distance(Rigid.apply(t, m[$0].object), m[$0].world) < tol($0) }
            guard inl.count >= bestInl.count else { break }
            bestInl = inl
        }
        return (pose, bestInl)
    }
}
