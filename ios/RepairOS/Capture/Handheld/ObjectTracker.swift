import Foundation
import simd

// The hand-held tracker: where the object is now (6 DoF), every tracked frame, from LiDAR depth and the colour image.
//
//   1. Mask: depth samples that are not the person (segmentation or hand-pose mask), not the table (world height
//      above the support plane), and inside the locked box where the prediction puts it (+3 cm).
//   2. Predict: constant velocity about the object's centre.
//   3. Features: FAST + steered binary tests on the masked colour image, lifted to 3D with depth, matched to the
//      object's feature bank near where the prediction puts them, random-sample consensus rigid fit.
//   4. ICP: point-to-plane against the accumulated model (coarse 12 mm level, then 4 mm), from the prediction and
//      from the feature pose, with the feature inliers as point-to-point terms in the same solve, so a symmetric or
//      flat object cannot slide. The better start wins on quality.
//   5. Quality 0…1 from inlier share, overlap, RMS and how well constrained the solve was (a slide direction with no
//      features to hold it lowers it). Low twice → lost: the model freezes and the app asks to hold it still.
//   6. Relocalise when lost: the last good pose, a whole-bank feature match, and 24 turns of the object about its
//      centre (ObjectAligner's idea, in 6 DoF), each refined by ICP; accepted only when two frames in a row agree.
//
// Prior art (ideas only, our own code): in-hand scanning with frame-to-model ICP and outlier handling (Weise et al.
// 2009), KinectFusion's projective point-to-plane tracking (Newcombe et al. 2011), joint geometric + feature terms
// (BundleSDF, Wen et al. 2023; its code is under a non-commercial licence and is not used).

/// One frame for the tracker, copied out of the ARFrame on the main queue (nothing of ARKit is kept).
struct TrackerFrame: Sendable {
    var timestamp: Double
    var cameraToWorld: simd_float4x4
    /// LiDAR depth (metres) and confidence (0/1/2), `depthWidth` × `depthHeight`, intrinsics at that size.
    var depth: [Float]
    var confidence: [UInt8]
    var depthWidth: Int, depthHeight: Int
    var depthK: SIMD4<Float>        // fx, fy, cx, cy
    /// Grey image (luma) and its intrinsics.
    var luma: GrayImage
    var lumaK: SIMD4<Float>
    /// 1 where a person (hand, arm) is, at depth resolution; nil when no mask is available.
    var personMask: [UInt8]?
    /// The support plane's world height (the table), nil when unknown.
    var planeY: Float?
}

struct TrackResult: Sendable {
    enum State: String, Sendable { case tracking, lost, recovered }
    var timestamp: Double
    var objectToWorld: simd_float4x4
    var quality: Float
    var state: State
    /// Fuse this frame into the cloud (tracking well).
    var fuse: Bool
    var points: Int = 0
    var inliers: Int = 0
    var rmsMM: Float = 0
    var featuresSeen: Int = 0
    var featureMatches: Int = 0
    var featureInliers: Int = 0
    var degenerate = false
    /// The weakest constrained direction's share of the strongest (under ObjectTracker.weakDirection = degenerate).
    var conditioning: Float = 1
    var start: String = ""
    var ms: Double = 0
    var modelPoints: Int = 0
    var bankFeatures: Int = 0
}

struct TrackerConfig: Sendable {
    var depthStep = 2
    var maxSourcePoints = 1600
    var maxFeatures = 260
    var boxMargin: Float = 0.03
    var minConfidence: UInt8 = 1
    var maxDepth: Float = 1.2
    var tableClearance: Float = 0.008
    var fuseQuality: Float = 0.55
    var lostQuality: Float = 0.33
    var relocQuality: Float = 0.6
    var featureWeight: Double = 25
    var useFeatures = true
    /// Try the 24 turns while lost every this many frames (the expensive part).
    var turnSearchEvery = 3
    /// Directions weaker than this share of the strongest keep the start pose (0 = solve every direction).
    var weakDirection = ObjectTracker.weakDirection
    static let standard = TrackerConfig()
}

final class ObjectTracker: @unchecked Sendable {
    let config: TrackerConfig
    private(set) var model: ObjectModel
    private(set) var bank = FeatureBank()
    let box: TrackBox
    private(set) var pose: simd_float4x4
    private(set) var state: TrackResult.State = .tracking
    private var history: [(t: Double, m: simd_float4x4)] = []
    private var bad = 0
    private var frameIndex = 0
    private var lostFrames = 0
    private var pendingReloc: simd_float4x4?
    private(set) var lostEvents = 0
    private(set) var recoveries = 0
    private var lastDegenerate = false

    /// `box`: the locked box (object frame = world at lock); `seed`: the scan's points in it at lock (object frame).
    init(box: TrackBox, seed: [SIMD3<Float>], start: simd_float4x4 = Rigid.identity, config: TrackerConfig = .standard) {
        self.box = box
        self.config = config
        model = ObjectModel(box: box)
        model.seed(seed.filter { $0.y > box.planeY + config.tableClearance * 0.5 })
        pose = start
    }

    /// Adds known surface that was never seen (FlipAligner: the table under the object, which its underside sat
    /// on), with its normal.
    func addSurface(_ pts: [SIMD3<Float>], normal: SIMD3<Float>) {
        for p in pts { model.add(p, normal: normal, weight: 0.5) }
    }

    // MARK: Per frame

    struct Source {
        var world: [SIMD3<Float>] = []
        var normal: [SIMD3<Float>] = []
        var depth: [Float] = []
        /// Pixel → kept (for the feature mask), at depth resolution.
        var mask: [UInt8] = []
    }

    func process(_ f: TrackerFrame) -> TrackResult {
        let t0 = Date()
        frameIndex += 1
        bank.nextFrame()
        let cam = Rigid.translation(f.cameraToWorld)
        let predicted = predict(to: f.timestamp)
        let lost = state == .lost
        let region: Region = lost ? .sphere(Rigid.apply(pose, model.centroid), model.radius + 0.06)
            : .box(Rigid.inverse(predicted), config.boxMargin)
        let src = sources(f, region: region)
        var result = TrackResult(timestamp: f.timestamp, objectToWorld: pose, quality: 0, state: state, fuse: false)
        result.points = src.world.count

        // Features.
        var seen: [(world: SIMD3<Float>, desc: Descriptor, depth: Float)] = []
        if config.useFeatures {
            seen = features(f, mask: src.mask)
            result.featuresSeen = seen.count
        }
        var matches: [FeatureMatch] = []
        var featPose: (objectToWorld: simd_float4x4, inliers: [Int])?
        if !seen.isEmpty, bank.count >= 6 {
            let speed = history.count >= 2 ? Rigid.distance(history[history.count - 1].m, history[history.count - 2].m,
                                                             probe: [model.centroid]) : 0
            matches = FeatureMatcher.match(seen, bank: bank, worldToObject: lost ? nil : Rigid.inverse(predicted),
                                           cameraWorld: cam, radius: min(0.08, 0.03 + 1.5 * speed))
            result.featureMatches = matches.count
            featPose = FeatureMatcher.consensusFit(matches, seed: UInt64(frameIndex))
            result.featureInliers = featPose?.inliers.count ?? 0
        }
        let inlierMatches = featPose.map { fp in fp.inliers.map { matches[$0] } } ?? []

        guard src.world.count >= 60 else {
            result.ms = Date().timeIntervalSince(t0) * 1000
            return finish(&result, best: nil, f: f, src: src, seen: seen)
        }

        // Starts for ICP.
        var starts: [(String, simd_float4x4)] = []
        if lost {
            starts.append(("last", pose))
            if let fp = featPose { starts.append(("features", fp.objectToWorld)) }
            if lostFrames % config.turnSearchEvery == 0 { starts += turnHypotheses(src) }
        } else {
            starts.append(("predicted", predicted))
            // A feature pose that disagrees with the prediction competes on its own (the prediction is then refined
            // without the feature pairs, so a wrong feature consensus cannot drag both).
            // It needs a clear consensus to compete (≥ 8 pairs, most of the matches): a few pairs on a small label
            // can agree on a wrong pose.
            if let fp = featPose, fp.inliers.count >= 8, Float(fp.inliers.count) >= 0.6 * Float(matches.count),
               Rigid.distance(fp.objectToWorld, predicted, probe: box.corners) > 0.015 {
                starts.append(("features", fp.objectToWorld))
            }
        }
        let thin = Self.thin(src, max: config.maxSourcePoints)
        var cands: [Candidate] = []
        if starts.count > 4 {
            // Many starts (the turn search): a short coarse pass on few points ranks them; the best three are refined.
            let few = Self.thin(src, max: 350)
            var ranked: [(String, simd_float4x4, Float)] = []
            for (name, s) in starts {
                let r = icp(few, start: s, matches: [], schedule: [(true, 0.03), (true, 0.025), (true, 0.02), (true, 0.015)])
                ranked.append((name, r.pose, r.quality))
            }
            ranked.sort { $0.2 > $1.2 }
            for (name, s, _) in ranked.prefix(3) { cands.append(refine(thin, start: s, matches: inlierMatches, name: name)) }
        } else {
            let split = !lost && starts.count == 2
            for (name, s) in starts {
                cands.append(refine(thin, start: s, matches: split && name == "predicted" ? [] : inlierMatches, name: name))
            }
            // Between disagreeing starts the fit decides (features cannot raise it) and a tie keeps the prediction,
            // unless the prediction leaves a direction unpinned that the features hold (a cylinder's spin: the fit
            // cannot see it, the label can).
            if split, cands.count == 2 {
                let p = cands[0], fe = cands[1]
                let featuresWin = fe.geometry > p.geometry + 0.05 || (p.degenerate && !fe.degenerate && fe.geometry >= p.geometry - 0.05)
                cands[featuresWin ? 0 : 1].quality = -1
            }
        }
        cands.sort { $0.quality > $1.quality }
        var best = cands.first
        // Relocalising: accepted on the fit (the reported quality keeps its cap). Another distinct pose about as
        // good (a symmetric object) is refused unless features hold the best one; the pose the object was lost in
        // (the person was asked to hold it still) is the tie-breaker when the fit is equal.
        if lost, var b = best {
            let rivals = cands.dropFirst().filter { Rigid.distance($0.pose, b.pose, probe: box.corners) > 0.02 && $0.geometry >= b.geometry - 0.06 }
            if !rivals.isEmpty, b.held < 8 {
                if let last = cands.first(where: { $0.start == "last" }), last.geometry >= b.geometry - 0.06 {
                    b = last
                } else {
                    b.geometry = min(b.geometry, config.relocQuality - 0.01)
                }
            }
            best = b
        }
        if let b = best {
            result.inliers = b.inliers
            result.rmsMM = b.rms * 1000
            result.degenerate = b.degenerate
            result.conditioning = b.ratio
            result.start = b.start
        }
        result.ms = Date().timeIntervalSince(t0) * 1000
        return finish(&result, best: best, f: f, src: src, seen: seen)
    }

    private func finish(_ r: inout TrackResult, best: Candidate?, f: TrackerFrame, src: Source,
                        seen: [(world: SIMD3<Float>, desc: Descriptor, depth: Float)]) -> TrackResult {
        let q = best?.quality ?? 0
        r.quality = q
        switch state {
        case .tracking, .recovered:
            if let best, q >= config.lostQuality {
                bad = 0
                pose = best.pose
                state = .tracking
            } else {
                bad += 1
                if bad >= 2 || q < 0.15 {
                    state = .lost
                    lostEvents += 1
                    lostFrames = 0
                    pendingReloc = nil
                    history = []
                } else if let best, q > 0.2 {
                    pose = best.pose   // shaky: follow it, but do not fuse
                }
            }
        case .lost:
            lostFrames += 1
            if let best, best.geometry >= config.relocQuality {
                if let prev = pendingReloc, Rigid.distance(prev, best.pose, probe: box.corners) < 0.015 {
                    state = .recovered
                    recoveries += 1
                    pose = best.pose
                    pendingReloc = nil
                    bad = 0
                    history = []
                } else {
                    pendingReloc = best.pose
                }
            } else {
                pendingReloc = nil
            }
        }
        r.state = state
        r.objectToWorld = pose
        if state != .lost {
            history.append((f.timestamp, pose))
            if history.count > 3 { history.removeFirst() }
        }
        lastDegenerate = best?.degenerate ?? false
        // Fuse and learn on the fit: along a direction nothing pins down (a plain cylinder's spin) the object looks
        // the same whatever the pose there, so the model stays consistent; the reported quality says it is unsure.
        r.fuse = (state == .tracking) && q >= config.lostQuality && (best?.geometry ?? 0) >= config.fuseQuality
        if r.fuse { learn(src: src, seen: seen, f: f) }
        r.modelPoints = model.count
        r.bankFeatures = bank.count
        return r
    }

    /// Grows the model and the feature bank from a well-tracked frame.
    private func learn(src: Source, seen: [(world: SIMD3<Float>, desc: Descriptor, depth: Float)], f: TrackerFrame) {
        let inv = Rigid.inverse(pose)
        let rot = Rigid.rotation(inv)
        let step = model.count < 3000 ? 1 : 2
        for i in stride(from: 0, to: src.world.count, by: step) {
            let q = Rigid.apply(inv, src.world[i])
            guard model.box.contains(q, margin: 0.004) else { continue }
            // Refine known surface (within 6 mm) or add a new one (nothing within ~1.2 cm); the band between is
            // inconsistent with the object (a finger, a mixed pixel) and is skipped.
            if model.fine.nearest(q, maxDist: 0.006) != nil || model.coarse.nearest(q, maxDist: 0.012) == nil {
                model.add(q, normal: rot * src.normal[i])
            }
        }
        let camObj = Rigid.apply(inv, Rigid.translation(f.cameraToWorld))
        for s in seen {
            let q = Rigid.apply(inv, s.world)
            guard model.box.contains(q, margin: 0.005) else { continue }
            bank.add(q, s.desc, seenFrom: simd_normalize(camObj - q))
        }
    }

    // MARK: Prediction

    func predict(to t: Double) -> simd_float4x4 {
        // After a frame whose pose was not fully pinned down, its velocity is not measured: hold still.
        guard history.count >= 2, !lastDegenerate else { return pose }
        let a = history[history.count - 2], b = history[history.count - 1]
        return Rigid.extrapolate(a.m, at: a.t, b.m, at: b.t, to: t, pivot: Rigid.apply(b.m, model.centroid))
    }

    // MARK: Masked depth

    enum Region { case box(simd_float4x4, Float), sphere(SIMD3<Float>, Float) }

    func sources(_ f: TrackerFrame, region: Region) -> Source {
        var s = Source()
        let w = f.depthWidth, h = f.depthHeight, step = config.depthStep
        s.mask = [UInt8](repeating: 0, count: w * h)
        let fx = f.depthK.x, fy = f.depthK.y, cx = f.depthK.z, cy = f.depthK.w
        let m = f.cameraToWorld
        let rot = Rigid.rotation(m)
        let nb = 3 * step
        @inline(__always) func camPoint(_ u: Int, _ v: Int, _ d: Float) -> SIMD3<Float> {
            SIMD3((Float(u) - cx) * d / fx, -(Float(v) - cy) * d / fy, -d)
        }
        @inline(__always) func person(_ u: Int, _ v: Int) -> Bool {
            guard let pm = f.personMask else { return false }
            // Dilated by one pixel: the hand's edge pixels mix hand and object depth.
            for dv in -1...1 { for du in -1...1 {
                let x = min(w - 1, max(0, u + du)), y = min(h - 1, max(0, v + dv))
                if pm[y * w + x] != 0 { return true }
            } }
            return false
        }
        for v in stride(from: 0, to: h, by: step) {
            for u in stride(from: 0, to: w, by: step) {
                let i = v * w + u
                let d = f.depth[i]
                guard d.isFinite, d > 0.08, d < config.maxDepth, f.confidence[i] >= config.minConfidence else { continue }
                if person(u, v) { continue }
                let pc = camPoint(u, v, d)
                let pw = Rigid.apply(m, pc)
                if let py = f.planeY, pw.y < py + config.tableClearance { continue }
                switch region {
                case .box(let worldToObject, let margin):
                    guard box.contains(Rigid.apply(worldToObject, pw), margin: margin) else { continue }
                case .sphere(let c, let r):
                    guard simd_distance(pw, c) < r else { continue }
                }
                // Normal from neighbours `nb` pixels away (wider than the step: LiDAR noise).
                guard u + nb < w, v + nb < h else { continue }
                let dx = f.depth[i + nb], dy = f.depth[i + nb * w]
                guard dx > 0.08, dy > 0.08, abs(dx - d) < 0.03, abs(dy - d) < 0.03 else { continue }
                let ax = camPoint(u + nb, v, dx) - pc, ay = camPoint(u, v + nb, dy) - pc
                var n = simd_cross(ax, ay)
                let l = simd_length(n)
                guard l > 1e-9 else { continue }
                n /= l
                if simd_dot(n, pc) > 0 { n = -n }   // towards the camera
                s.world.append(pw)
                s.normal.append(rot * n)
                s.depth.append(d)
                for dv in 0..<step { for du in 0..<step where v + dv < h && u + du < w { s.mask[(v + dv) * w + u + du] = 1 } }
            }
        }
        return s
    }

    static func thin(_ s: Source, max n: Int) -> Source {
        guard s.world.count > n else { return s }
        var o = Source()
        let step = Double(s.world.count) / Double(n)
        for k in 0..<n {
            let i = Int(Double(k) * step)
            o.world.append(s.world[i])
            o.normal.append(s.normal[i])
            o.depth.append(s.depth[i])
        }
        o.mask = s.mask
        return o
    }

    // MARK: Features

    func features(_ f: TrackerFrame, mask: [UInt8]) -> [(world: SIMD3<Float>, desc: Descriptor, depth: Float)] {
        let lw = f.luma.width, lh = f.luma.height, dw = f.depthWidth, dh = f.depthHeight
        // The mask's bounds in luma pixels.
        var minX = dw, minY = dh, maxX = -1, maxY = -1
        for y in 0..<dh { for x in 0..<dw where mask[y * dw + x] != 0 {
            minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
        } }
        guard maxX > minX, maxY > minY else { return [] }
        let sx = Float(lw) / Float(dw), sy = Float(lh) / Float(dh)
        let region = (Int(Float(minX) * sx) - 4, Int(Float(minY) * sy) - 4, Int(Float(maxX + 1) * sx) + 4, Int(Float(maxY + 1) * sy) + 4)
        let kps = FeatureDetector.detect(f.luma, region: region, maxCount: config.maxFeatures) { x, y in
            let u = min(dw - 1, Int(Float(x) / sx)), v = min(dh - 1, Int(Float(y) / sy))
            return mask[v * dw + u] != 0
        }
        let fx = f.depthK.x, fy = f.depthK.y, cx = f.depthK.z, cy = f.depthK.w
        var out: [(world: SIMD3<Float>, desc: Descriptor, depth: Float)] = []
        for k in kps {
            // The keypoint's depth pixel (luma and depth share the camera; scale the coordinates).
            let u = k.x / sx, v = k.y / sy
            let iu = Int(u), iv = Int(v)
            guard iu >= 1, iv >= 1, iu < dw - 1, iv < dh - 1 else { continue }
            var lo: Float = .greatestFiniteMagnitude, hi: Float = 0, sum: Float = 0, n: Float = 0
            for dy in -1...1 { for dx in -1...1 {
                let d = f.depth[(iv + dy) * dw + iu + dx]
                guard d > 0.08, d.isFinite else { continue }
                lo = min(lo, d); hi = max(hi, d); sum += d; n += 1
            } }
            // On a depth edge the 3D point is unreliable: skip it.
            guard n >= 7, hi - lo < 0.01 else { continue }
            let d = sum / n
            let pc = SIMD3((u - cx) * d / fx, -(v - cy) * d / fy, -d)
            out.append((Rigid.apply(f.cameraToWorld, pc), k.descriptor, d))
        }
        return out
    }

    // MARK: ICP

    struct Candidate {
        var pose: simd_float4x4
        var quality: Float
        /// The fit alone (before the cap for a direction nothing pins down).
        var geometry: Float
        var inliers: Int
        var rms: Float
        var degenerate: Bool
        var ratio: Float
        var held: Int
        var start: String
    }

    private func refine(_ src: Source, start: simd_float4x4, matches: [FeatureMatch], name: String) -> Candidate {
        let r = icp(src, start: start, matches: matches,
                    schedule: [(true, 0.03), (true, 0.02), (true, 0.015), (false, 0.012), (false, 0.009), (false, 0.007),
                               (false, 0.006), (false, 0.006), (false, 0.005), (false, 0.005)])
        return Candidate(pose: r.pose, quality: r.quality, geometry: r.geometry, inliers: r.inliers, rms: r.rms,
                         degenerate: r.degenerate, ratio: r.ratio, held: r.held, start: name)
    }

    struct ICPOut { var pose: simd_float4x4; var quality: Float; var geometry: Float; var inliers: Int; var rms: Float; var degenerate: Bool; var ratio: Float; var held: Int }

    /// Directions the data does not pin down (an eigenvalue under this share of the largest) are not moved: they
    /// keep the start pose (degeneracy-aware solve: a cylinder's spin about its axis, a flat face's slide).
    static let weakDirection = 0.06
    /// Feature pairs that must hold the final pose for a weak geometric direction to count as pinned down.
    static let minHeldFeatures = 4

    /// Point-to-plane ICP (6 DoF) of world points to the model, with optional feature pairs as point-to-point terms.
    /// `schedule`: (use the coarse 12 mm level, correspondence distance) per iteration. Works on T = world → object:
    /// q' = R(q − c) + c + v about the model centroid c; the rotation is solved as arc length at the object's radius
    /// so rotation and translation compare in metres.
    func icp(_ src: Source, start: simd_float4x4, matches: [FeatureMatch], schedule: [(Bool, Float)]) -> ICPOut {
        var T = Rigid.inverse(start)
        let c = model.centroid
        let radius = Double(model.radius)
        var ratio: Double = 1
        var ran = false
        var weakDirections = 0
        for (coarse, maxDist) in schedule {
            let level = coarse ? model.coarse : model.fine
            var H = [Double](repeating: 0, count: 36), g = [Double](repeating: 0, count: 6)
            var used = 0
            let R = Rigid.rotation(T)
            let huber: Float = coarse ? 0.008 : 0.004
            for i in 0..<src.world.count {
                let q = Rigid.apply(T, src.world[i])
                guard let nn = level.nearest(q, maxDist: maxDist) else { continue }
                let mpt = level.position[nn.index], n = level.normal[nn.index]
                guard simd_length_squared(n) > 0.5, simd_dot(n, R * src.normal[i]) > 0.25 else { continue }
                let r = simd_dot(n, q - mpt)
                let w = Double(abs(r) <= huber ? 1 : huber / abs(r))
                let cr = simd_cross(q - c, n)
                let j: [Double] = [Double(cr.x) / radius, Double(cr.y) / radius, Double(cr.z) / radius, Double(n.x), Double(n.y), Double(n.z)]
                for a in 0..<6 {
                    g[a] += w * j[a] * Double(r)
                    for b in a..<6 { H[a * 6 + b] += w * j[a] * j[b] }
                }
                used += 1
            }
            guard used >= 30 else { break }
            for a in 0..<6 { for b in 0..<a { H[a * 6 + b] = H[b * 6 + a] } }
            // Geometry pins down only its strong directions (a cylinder's spin, a flat face's slide are weak: there
            // the normals' noise is all the "constraint"). Keep geometry in its strong subspace; the features, when
            // there are enough, hold the rest; else the weak directions keep the start pose.
            let (values, vectors) = DenseSolve.symmetricEigen(H, n: 6)
            guard let mx = values.max(), mx > 0 else { break }
            ratio = max(0, (values.min() ?? 0) / mx)
            ran = true
            var A = [Double](repeating: 0, count: 36), b = [Double](repeating: 0, count: 6)
            var weak = 0
            for k in 0..<6 {
                var proj = 0.0
                for a in 0..<6 { proj += vectors[a * 6 + k] * g[a] }
                let strong = values[k] > config.weakDirection * mx
                if !strong { weak += 1 }
                let lam = strong ? values[k] : (matches.count >= Self.minHeldFeatures ? 0 : mx)
                for a in 0..<6 {
                    if strong { b[a] += proj * vectors[a * 6 + k] }
                    for c2 in 0..<6 { A[a * 6 + c2] += lam * vectors[a * 6 + k] * vectors[c2 * 6 + k] }
                }
            }
            weakDirections = weak
            var Hf = [Double](repeating: 0, count: 36), gf = [Double](repeating: 0, count: 6)
            for fm in matches {
                let q = Rigid.apply(T, fm.world)
                let e = q - fm.object
                let l = simd_length(e)
                let k: Float = 0.01
                guard l < 0.05 else { continue }
                addPoint(&Hf, &gf, d: (q - c) / Float(radius), e: e, weight: config.featureWeight * Double(l <= k ? 1 : k / l))
            }
            for a in 0..<6 {
                b[a] += gf[a]
                for c2 in a..<6 { A[a * 6 + c2] += Hf[a * 6 + c2]; if c2 != a { A[c2 * 6 + a] += Hf[a * 6 + c2] } }
                A[a * 6 + a] += 1e-9 * mx + 1e-12
            }
            guard let x = DenseSolve.cholesky(A, b.map { -$0 }, n: 6) else { break }
            let w = SIMD3<Float>(Float(x[0] / radius), Float(x[1] / radius), Float(x[2] / radius))
            let v = SIMD3<Float>(Float(x[3]), Float(x[4]), Float(x[5]))
            let dr = Rigid.rotation(vector: w)
            T = Rigid.orthonormalized(Rigid.make(dr, c + v - dr * c) * T)
            if simd_length(w) * Float(radius) < 2e-6, simd_length(v) < 2e-6, !coarse { break }
        }
        let pose = Rigid.inverse(T)
        let s = score(src, worldToObject: T)
        let held = matches.filter { simd_distance(Rigid.apply(T, $0.world), $0.object) < 0.01 }.count
        let degenerate = ran && weakDirections > 0 && held < Self.minHeldFeatures && config.weakDirection > 0
        var q = s.quality
        // A direction nothing pins down: the pose there is only the prediction; not good enough to fuse.
        if degenerate { q = min(q, 0.5) }
        if matches.count >= 8, Float(held) < 0.3 * Float(matches.count) { q *= 0.85 }
        return ICPOut(pose: pose, quality: q, geometry: s.quality, inliers: s.inliers, rms: s.rms, degenerate: degenerate, ratio: Float(ratio), held: held)
    }

    @inline(__always)
    private func addPoint(_ H: inout [Double], _ g: inout [Double], d: SIMD3<Float>, e: SIMD3<Float>, weight w: Double) {
        // ∂q'/∂ω = −[d]×, ∂q'/∂v = I.
        let rows: [([Double], Double)] = [
            ([0, Double(d.z), Double(-d.y), 1, 0, 0], Double(e.x)),
            ([Double(-d.z), 0, Double(d.x), 0, 1, 0], Double(e.y)),
            ([Double(d.y), Double(-d.x), 0, 0, 0, 1], Double(e.z)),
        ]
        for (j, r) in rows {
            for a in 0..<6 where j[a] != 0 {
                g[a] += w * j[a] * r
                for b in a..<6 where j[b] != 0 { H[a * 6 + b] += w * j[a] * j[b] }
            }
        }
    }

    /// Inlier share (point-to-plane residual under 6 mm within 1 cm), overlap and RMS → quality 0…1.
    func score(_ src: Source, worldToObject T: simd_float4x4) -> (quality: Float, inliers: Int, rms: Float) {
        var n = 0, sum: Float = 0
        let R = Rigid.rotation(T)
        for i in 0..<src.world.count {
            let q = Rigid.apply(T, src.world[i])
            guard let nn = model.fine.nearest(q, maxDist: 0.01) else { continue }
            let nrm = model.fine.normal[nn.index]
            guard simd_dot(nrm, R * src.normal[i]) > 0.25 else { continue }
            let r = abs(simd_dot(nrm, q - model.fine.position[nn.index]))
            guard r < 0.006 else { continue }
            n += 1
            sum += r * r
        }
        guard n > 0 else { return (0, 0, 1) }
        let rms = (sum / Float(n)).squareRoot()
        let share = Float(n) / Float(max(1, src.world.count))
        let overlap = min(1, Float(n) / 220)
        let rmsFactor = rms < 0.0025 ? 1 : max(0.3, 1 - (rms - 0.0025) / 0.005)
        return (min(1, pow(share, 0.7) * overlap * rmsFactor * 1.08), n, rms)
    }

    // MARK: Relocalising

    /// 24 orientations (the cube's rotation group) about the object's centre, placed at the centroid of what is seen.
    func turnHypotheses(_ src: Source) -> [(String, simd_float4x4)] {
        guard !src.world.isEmpty else { return [] }
        let seen = src.world.reduce(.zero, +) / Float(src.world.count)
        let c = model.centroid
        // The visible side's centroid sits nearer the camera than the object's centre: no correction (ICP moves it).
        var out: [(String, simd_float4x4)] = []
        for (k, r) in Self.cubeRotations.enumerated() {
            // Keep the last known orientation as the base: turns relative to it.
            let base = Rigid.rotation(pose)
            let rot = base * r
            out.append(("turn\(k)", Rigid.make(rot, seen - rot * c)))
        }
        return out
    }

    static let cubeRotations: [simd_float3x3] = {
        var out: [simd_float3x3] = []
        let axes: [SIMD3<Float>] = [SIMD3(1, 0, 0), SIMD3(-1, 0, 0), SIMD3(0, 1, 0), SIMD3(0, -1, 0), SIMD3(0, 0, 1), SIMD3(0, 0, -1)]
        for up in axes {
            for fwd in axes where abs(simd_dot(up, fwd)) < 0.5 {
                let right = simd_cross(up, fwd)
                out.append(simd_float3x3(columns: (right, up, fwd)))
            }
        }
        return out
    }()
}
