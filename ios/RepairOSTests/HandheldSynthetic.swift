import Foundation
import simd
#if canImport(RepairOS)
@testable import RepairOS
#endif

/// Synthetic hand-held scans for the tracker: a textured object turned in front of a nearly still phone, ray-cast
/// into a 256 × 192 LiDAR depth map (Gaussian noise, metres) and a 640 × 480 grey image, with the hand as a sphere
/// stuck to the object (in the person mask), the table and a wall behind. Ground truth: the object's pose per frame.
/// Shared by the unit tests and the Mac replay harness (tools/handheld/).
struct SyntheticScene {
    enum Shape { case texturedBox, cylinderWithPatch }

    let shape: Shape
    let planeY: Float = 0.75
    /// The box at lock (object frame = world at lock).
    let box: TrackBox
    let depthW = 256, depthH = 192
    let lumaW = 640, lumaH = 480
    let depthK = SIMD4<Float>(190.7, 190.7, 128, 96)
    let lumaK = SIMD4<Float>(476.7, 476.7, 320, 240)
    var noise: Float = 0.003
    var handRadius: Float = 0.028
    /// False: the hand is in the depth but not in the person mask (segmentation missed it).
    var maskHand = true

    init(_ shape: Shape) {
        self.shape = shape
        switch shape {
        case .texturedBox:
            box = TrackBox(center: SIMD3(0, 0.75 + 0.03, -0.35), half: SIMD3(0.045, 0.03, 0.03), yaw: 0.3)
        case .cylinderWithPatch:
            box = TrackBox(center: SIMD3(0, 0.75 + 0.05, -0.35), half: SIMD3(0.035, 0.05, 0.035), yaw: 0)
        }
    }

    // MARK: Motion

    /// The object's pose at time t (s): resting for `rest` s, lifted 12 cm over 0.8 s, then turned about a slowly
    /// changing axis at `rate` rad/s (a hand turning it), including a flip.
    static func objectPose(_ t: Double, box: TrackBox, rate: Float = 0.8, rest: Double = 0.6) -> simd_float4x4 {
        let c = box.center
        guard t > rest else { return Rigid.identity }
        let lift = Float(min(1, (t - rest) / 0.8))
        let s = Float(t - rest)
        // Turn about an axis that wanders from vertical towards horizontal (so the underside comes into view).
        let axis = simd_normalize(SIMD3<Float>(0.6 * sin(0.5 * s), 1, 0.9 * sin(0.33 * s + 0.4) + 0.3 * s))
        let angle = rate * max(0, s - 0.4)
        let r = simd_float3x3(simd_quatf(angle: angle, axis: axis))
        let lifted = c + SIMD3(0.01 * sin(1.3 * s), 0.12 * lift, 0.02 * lift)
        return Rigid.make(r, lifted - r * c)
    }

    /// The camera: 35 cm in front of and above the object, swaying a little (the phone is hand-held too).
    func cameraPose(_ t: Double) -> simd_float4x4 {
        let s = Float(t)
        let eye = SIMD3<Float>(0.02 * sin(0.7 * s), planeY + 0.30 + 0.01 * sin(1.1 * s), -0.35 + 0.30 + 0.01 * cos(0.9 * s))
        let target = box.center + SIMD3(0, 0.08, 0)
        let fwd = simd_normalize(target - eye)
        let right = simd_normalize(simd_cross(fwd, SIMD3(0, 1, 0)))
        let up = simd_cross(right, fwd)
        // ARKit camera: x right, y up, looks down −z.
        return simd_float4x4(columns: (SIMD4(right, 0), SIMD4(up, 0), SIMD4(-fwd, 0), SIMD4(eye, 1)))
    }

    // MARK: Ray casting

    /// First hit along a world ray: distance along the ray, the grey value, and whether it is the hand.
    func cast(origin o: SIMD3<Float>, dir d: SIMD3<Float>, objectToWorld W: simd_float4x4, worldToObject inv: simd_float4x4,
              hand: SIMD3<Float>?) -> (t: Float, grey: Float, hand: Bool)? {
        var best: (t: Float, grey: Float, hand: Bool)?
        func take(_ t: Float, _ g: Float, _ h: Bool) {
            if t > 0.01, t < (best?.t ?? .greatestFiniteMagnitude) { best = (t, g, h) }
        }
        // Table (world) and wall.
        if d.y < -1e-6 {
            let t = (planeY - o.y) / d.y
            let p = o + t * d
            take(t, 90 + 30 * Self.noise3(p * 40), false)
        }
        if d.z < -1e-6 {
            let t = (-0.95 - o.z) / d.z
            take(t, 150, false)
        }
        // Object (in its own frame).
        let oo = Rigid.apply(inv, o), od = Rigid.rotation(inv) * d
        if let (t, p, n) = intersectObject(oo, od) {
            let view = abs(simd_dot(simd_normalize(n), -od))
            take(t, texture(p) * (0.55 + 0.45 * view), false)
        }
        if let hc = hand {
            // Sphere.
            let oc = o - hc
            let b = simd_dot(oc, d), cc = simd_dot(oc, oc) - handRadius * handRadius
            let disc = b * b - cc
            if disc > 0 {
                let t = -b - disc.squareRoot()
                take(t, 170, true)
            }
        }
        return best
    }

    /// Ray in the object frame → (t, point, normal) on the object.
    func intersectObject(_ o: SIMD3<Float>, _ d: SIMD3<Float>) -> (Float, SIMD3<Float>, SIMD3<Float>)? {
        switch shape {
        case .texturedBox:
            let lo = box.local(o)
            let cr = cos(-box.yaw), sr = sin(-box.yaw)
            let ld = SIMD3(cr * d.x + sr * d.z, d.y, -sr * d.x + cr * d.z)
            var tmin: Float = -.greatestFiniteMagnitude, tmax: Float = .greatestFiniteMagnitude
            var axis = 0
            for i in 0..<3 {
                if abs(ld[i]) < 1e-9 {
                    if abs(lo[i]) > box.half[i] { return nil }
                    continue
                }
                var t1 = (-box.half[i] - lo[i]) / ld[i], t2 = (box.half[i] - lo[i]) / ld[i]
                if t1 > t2 { swap(&t1, &t2) }
                if t1 > tmin { tmin = t1; axis = i }
                tmax = min(tmax, t2)
            }
            guard tmin <= tmax, tmin > 0 else { return nil }
            var nl = SIMD3<Float>.zero
            nl[axis] = ld[axis] > 0 ? -1 : 1
            let n = simd_quatf(angle: box.yaw, axis: SIMD3(0, 1, 0)).act(nl)
            return (tmin, o + tmin * d, n)
        case .cylinderWithPatch:
            let c = box.center, r = box.half.x, h = box.half.y
            let po = o - c
            var best: (Float, SIMD3<Float>, SIMD3<Float>)?
            let a = d.x * d.x + d.z * d.z, b = 2 * (po.x * d.x + po.z * d.z), cc = po.x * po.x + po.z * po.z - r * r
            let disc = b * b - 4 * a * cc
            if a > 1e-9, disc > 0 {
                let t = (-b - disc.squareRoot()) / (2 * a)
                let p = po + t * d
                if t > 0, abs(p.y) <= h { best = (t, c + p, SIMD3(p.x, 0, p.z) / r) }
            }
            for cap in [Float(-1), 1] where abs(d.y) > 1e-9 {
                let t = (cap * h - po.y) / d.y
                let p = po + t * d
                if t > 0, p.x * p.x + p.z * p.z <= r * r, t < (best?.0 ?? .greatestFiniteMagnitude) {
                    best = (t, c + p, SIMD3(0, cap, 0))
                }
            }
            return best
        }
    }

    /// Grey value of the object's surface at an object-frame point.
    func texture(_ p: SIMD3<Float>) -> Float {
        switch shape {
        case .texturedBox:
            let v = Self.noise3(p * 110) * 0.7 + Self.noise3(p * 260) * 0.3
            return 128 + 105 * tanh((v - 0.5) * 9)
        case .cylinderWithPatch:
            // Plain, except a 4 cm patch on one side (a label) and a mark on the top.
            let q = p - box.center
            let ang = atan2(q.z, q.x)
            if ang > -0.25, ang < 0.95, abs(q.y) < 0.03 {
                let v = Self.noise3(p * 120)
                return 128 + 105 * tanh((v - 0.5) * 9)
            }
            if q.y > box.half.y - 0.001, q.x > 0, q.x < 0.025, abs(q.z) < 0.006 { return 230 }
            return 140
        }
    }

    /// Deterministic value noise in [0, 1].
    static func noise3(_ p: SIMD3<Float>) -> Float {
        let i = SIMD3<Int32>(Int32((p.x).rounded(.down)), Int32((p.y).rounded(.down)), Int32((p.z).rounded(.down)))
        let f = p - SIMD3<Float>(Float(i.x), Float(i.y), Float(i.z))
        let u = f * f * (3 - 2 * f)
        func h(_ x: Int32, _ y: Int32, _ z: Int32) -> Float {
            var k = UInt32(bitPattern: x &* 73_856_093 ^ y &* 19_349_663 ^ z &* 83_492_791)
            k ^= k >> 13; k = k &* 0x5BD1_E995; k ^= k >> 15
            return Float(k & 0xFFFF) / 65535
        }
        func lerp(_ a: Float, _ b: Float, _ t: Float) -> Float { a + (b - a) * t }
        let x0 = lerp(lerp(h(i.x, i.y, i.z), h(i.x + 1, i.y, i.z), u.x), lerp(h(i.x, i.y + 1, i.z), h(i.x + 1, i.y + 1, i.z), u.x), u.y)
        let x1 = lerp(lerp(h(i.x, i.y, i.z + 1), h(i.x + 1, i.y, i.z + 1), u.x),
                      lerp(h(i.x, i.y + 1, i.z + 1), h(i.x + 1, i.y + 1, i.z + 1), u.x), u.y)
        return lerp(x0, x1, u.z)
    }

    // MARK: Frames

    /// The hand: a sphere touching the object's side, moving with it (nil = no hand).
    func handCentre(_ W: simd_float4x4) -> SIMD3<Float> {
        let side = box.center + SIMD3(box.half.x + handRadius * 0.85, -box.half.y * 0.3, 0.005)
        return Rigid.apply(W, side)
    }

    func frame(at t: Double, objectToWorld W: simd_float4x4, withHand: Bool, seed: UInt64,
               hideObject: Bool = false) -> TrackerFrame {
        let C = cameraPose(t)
        let hand = withHand && !hideObject ? handCentre(W) : nil
        let o = Rigid.translation(C), R = Rigid.rotation(C)
        var rng = SplitMix64(seed: seed)
        var depth = [Float](repeating: 0, count: depthW * depthH)
        var conf = [UInt8](repeating: 0, count: depthW * depthH)
        var mask = [UInt8](repeating: 0, count: depthW * depthH)
        let farW = hideObject ? simd_float4x4(columns: (SIMD4(1, 0, 0, 0), SIMD4(0, 1, 0, 0), SIMD4(0, 0, 1, 0), SIMD4(0, 5, 0, 1))) : W
        let farInv = Rigid.inverse(farW)
        for v in 0..<depthH {
            for u in 0..<depthW {
                let dc = SIMD3((Float(u) - depthK.z) / depthK.x, -(Float(v) - depthK.w) / depthK.y, -1)
                let dw = R * simd_normalize(dc)
                guard let hit = cast(origin: o, dir: dw, objectToWorld: farW, worldToObject: farInv, hand: hand) else { continue }
                // Depth is along −z: t × cos(angle to the axis).
                let z = hit.t / simd_length(dc)
                let i = v * depthW + u
                depth[i] = max(0.01, z + noise * rng.gaussian())
                conf[i] = 2
                if hit.hand { mask[i] = 1 }
            }
        }
        var luma = [UInt8](repeating: 0, count: lumaW * lumaH)
        for v in 0..<lumaH {
            for u in 0..<lumaW {
                let dc = SIMD3((Float(u) - lumaK.z) / lumaK.x, -(Float(v) - lumaK.w) / lumaK.y, -1)
                let dw = R * simd_normalize(dc)
                let g = cast(origin: o, dir: dw, objectToWorld: farW, worldToObject: farInv, hand: hand)?.grey ?? 40
                luma[v * lumaW + u] = UInt8(max(0, min(255, g + 2 * rng.gaussian())))
            }
        }
        return TrackerFrame(timestamp: t, cameraToWorld: C, depth: depth, confidence: conf, depthWidth: depthW, depthHeight: depthH,
                            depthK: depthK, luma: GrayImage(width: lumaW, height: lumaH, pixels: luma), lumaK: lumaK,
                            personMask: withHand && maskHand ? mask : nil, planeY: planeY)
    }

    /// The object's surface points seen at lock (from the first camera), object frame: the tracker's seed.
    func seedPoints() -> [SIMD3<Float>] {
        var pts: [SIMD3<Float>] = []
        for (k, t) in [0.0, 0.3].enumerated() {
            var f = frame(at: t, objectToWorld: Rigid.identity, withHand: false, seed: 900 + UInt64(k))
            f.personMask = nil
            for v in stride(from: 0, to: depthH, by: 2) {
                for u in stride(from: 0, to: depthW, by: 2) {
                    let d = f.depth[v * depthW + u]
                    guard d > 0 else { continue }
                    let pc = SIMD3((Float(u) - depthK.z) * d / depthK.x, -(Float(v) - depthK.w) * d / depthK.y, -d)
                    let pw = Rigid.apply(f.cameraToWorld, pc)
                    if box.contains(pw, margin: 0.003), pw.y > planeY + 0.004 { pts.append(pw) }
                }
            }
        }
        return pts
    }

    /// Pose error: the largest displacement of the box corners (m) and the rotation error (degrees).
    func error(_ est: simd_float4x4, _ gt: simd_float4x4) -> (mm: Float, deg: Float) {
        (Rigid.distance(est, gt, probe: box.corners) * 1000, Rigid.angle(est, gt) * 180 / .pi)
    }
}

/// Runs a synthetic sequence through the tracker and reports per-frame errors.
struct SyntheticRun {
    struct Row { var t: Double; var mm: Float; var deg: Float; var quality: Float; var state: TrackResult.State; var ms: Double }
    var rows: [Row] = []
    var meanMM: Float { rows.isEmpty ? 0 : rows.map(\.mm).reduce(0, +) / Float(rows.count) }
    var maxMM: Float { rows.map(\.mm).max() ?? 0 }
    var meanDeg: Float { rows.isEmpty ? 0 : rows.map(\.deg).reduce(0, +) / Float(rows.count) }
    var maxDeg: Float { rows.map(\.deg).max() ?? 0 }
    var turnedDeg: Float = 0

    /// `hz`: tracker rate; `frames`; `occlude`: frame indices where the object is out of view (lost/recovered test).
    static func run(_ scene: SyntheticScene, hz: Double = 15, frames: Int, rate: Float = 0.8, withHand: Bool = true,
                    occlude: Range<Int>? = nil, config: TrackerConfig = .standard, onResult: ((Int, TrackResult) -> Void)? = nil)
        -> SyntheticRun {
        let tracker = ObjectTracker(box: scene.box, seed: scene.seedPoints(), config: config)
        var run = SyntheticRun()
        for k in 0..<frames {
            let t = Double(k) / hz
            let gt = SyntheticScene.objectPose(t, box: scene.box, rate: rate)
            let hidden = occlude?.contains(k) ?? false
            let f = scene.frame(at: t, objectToWorld: gt, withHand: withHand, seed: UInt64(k + 1), hideObject: hidden)
            let r = tracker.process(f)
            onResult?(k, r)
            let e = scene.error(r.objectToWorld, gt)
            if !hidden, r.state != .lost {
                run.rows.append(Row(t: t, mm: e.mm, deg: e.deg, quality: r.quality, state: r.state, ms: r.ms))
            }
            run.turnedDeg = Rigid.angle(gt, Rigid.identity) * 180 / .pi
        }
        return run
    }
}

/// Synthetic "Scan the bottom": the main scan's cloud (top and sides) and a mini-scan of the same object flipped
/// over (bottom and sides) in a different ARKit session, with 2 mm noise and patchy coverage, and the true
/// mini-scan → main-scan transform.
enum SyntheticFlip {
    enum Shape { case texturedBox, flatBoard }

    struct Case { var target: FlipCloud; var source: FlipCloud; var truth: simd_float4x4 }

    static func make(_ shape: Shape, seed: UInt64, flipAboutZ: Bool = false, noise: Float = 0.002) -> Case {
        var rng = SplitMix64(seed: seed)
        let half: SIMD3<Float> = shape == .texturedBox ? SIMD3(0.045, 0.03, 0.03) : SIMD3(0.07, 0.008, 0.045)
        // Surface samples every 2.5 mm, object frame (centred), with outward normals and colours.
        var surf: [(p: SIMD3<Float>, n: SIMD3<Float>, c: SIMD3<Float>)] = []
        let step: Float = 0.0025
        for axis in 0..<3 {
            for sgn in [Float(-1), 1] {
                let u = (axis + 1) % 3, v = (axis + 2) % 3
                var a = -half[u]
                while a <= half[u] {
                    var b = -half[v]
                    while b <= half[v] {
                        var p = SIMD3<Float>.zero
                        p[axis] = sgn * half[axis]; p[u] = a; p[v] = b
                        var n = SIMD3<Float>.zero
                        n[axis] = sgn
                        surf.append((p, n, colour(shape, p, half: half)))
                        b += step
                    }
                    a += step
                }
            }
        }
        let planeY: Float = 0.75
        func pose(yaw: Float, flip: Bool, at xz: SIMD2<Float>) -> simd_float4x4 {
            let f = flip ? simd_float3x3(simd_quatf(angle: .pi, axis: flipAboutZ ? SIMD3(0, 0, 1) : SIMD3(1, 0, 0))) : matrix_identity_float3x3
            let r = simd_float3x3(simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))) * f
            return Rigid.make(r, SIMD3(xz.x, planeY + half.y, xz.y))
        }
        func scan(_ P: simd_float4x4, yaw: Float) -> FlipCloud {
            var pts: [SIMD3<Float>] = [], cols: [SIMD3<Float>] = []
            let R = Rigid.rotation(P)
            // Patchy coverage: a few sides seen less (the mini-scan is quick).
            let keep = Float.random(in: 0.55...0.85, using: &rng)
            for s in surf {
                let n = R * s.n
                guard n.y > -0.3 else { continue }            // the face on the table is not seen
                guard Float.random(in: 0..<1, using: &rng) < keep else { continue }
                let p = Rigid.apply(P, s.p) + n * (noise * rng.gaussian())
                pts.append(p)
                cols.append(simd_clamp(s.c + SIMD3(repeating: 0.03 * rng.gaussian()), .zero, SIMD3(repeating: 1)))
            }
            let c = Rigid.translation(P)
            // The locked box: upright, padded like the lock (×1.15) with its bottom on the plane, yaw from the pose.
            let h = half * 1.15
            return FlipCloud(points: pts, colors: cols, box: TrackBox(center: SIMD3(c.x, planeY + h.y, c.z), half: h, yaw: yaw))
        }
        let P0 = pose(yaw: 0.3, flip: false, at: SIMD2(0, -0.35))
        let yawK = Float.random(in: 0..<(2 * .pi), using: &rng)
        let Pk = pose(yaw: yawK, flip: true, at: SIMD2(Float.random(in: -0.1...0.1, using: &rng), Float.random(in: -0.5...(-0.3), using: &rng)))
        // The flipped object's upright box: its yaw is the pose's turn about the vertical.
        let xk = Rigid.rotation(Pk).columns.0
        let boxYaw = atan2(-xk.z, xk.x)
        return Case(target: scan(P0, yaw: 0.3), source: scan(Pk, yaw: boxYaw), truth: P0 * Rigid.inverse(Pk))
    }

    static func colour(_ shape: Shape, _ p: SIMD3<Float>, half: SIMD3<Float>) -> SIMD3<Float> {
        switch shape {
        case .texturedBox:
            let v = SyntheticScene.noise3(p * 90), w = SyntheticScene.noise3(p * 90 + 17)
            return SIMD3(0.2 + 0.6 * v, 0.3 + 0.4 * w, 0.5 - 0.3 * v)
        case .flatBoard:
            // A plain board (green, like a circuit board) with one sticker on an end, off centre: the only thing
            // that tells its flips apart.
            if p.x > half.x - 0.001, p.z > 0.005, p.z < 0.03 { return SIMD3(0.9, 0.15, 0.1) }
            return SIMD3(0.15, 0.45, 0.2)
        }
    }

    /// Error of an estimate: the largest displacement of the mini-scan box's corners (m).
    static func error(_ est: simd_float4x4, _ c: Case) -> Float { Rigid.distance(est, c.truth, probe: c.source.box.corners) }
}
