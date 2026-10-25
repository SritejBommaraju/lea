import ARKit
import Foundation
import simd

/// "Turn it": the person holds the object up and turns it (or turns it on the table, or flips it for the underside)
/// while the phone stays roughly still. This drives the tracker (ObjectTracker) from ARKit frames and gives the scan
/// everything in the OBJECT frame (= ARKit world at the moment of the lock, when the object rested on the table):
/// the camera relative to the object for stills, keyframes, the coverage dial and the framing gate; the cloud fused
/// in the object frame on the GPU (HandheldFusion); a pose source and quality for every record.
///
/// Threading: `feed` runs on the main queue (the AR delegate), copies ≤ 15 frames a second (no ARFrame is kept) and
/// hands them to one serial background queue where the tracker runs; results come back to the main queue.
@MainActor
final class HandheldController {
    nonisolated static let poseSource = "object_tracked"
    /// The tracker's rate: every 4th frame at 60 fps.
    static let interval: Double = 1.0 / 15
    /// Pose quality below which a still or keyframe is not taken (its pose would not be trusted).
    nonisolated static let recordQuality: Float = 0.55

    let box: ObjectBox
    let diag: Diag
    private let queue = DispatchQueue(label: "handheld-tracker", qos: .userInitiated)
    private let tracker: ObjectTracker
    let fusion: HandheldFusion?
    private let planeY: Float?
    private let useSegmentation: Bool
    private let useHandPose: Bool
    private let rawDir: URL?

    private var busy = false
    private var lastFed: Double = 0
    private var frameIndex = 0
    private var rawWritten = 0
    private(set) var latest: TrackResult?
    /// Tracked poses (not lost), newest last, for the display extrapolation.
    private var poses: [(t: Double, m: simd_float4x4)] = []
    private let started = Date()
    private var lastStats = Date()
    private var trackMS: [Double] = []
    private var copyMS: [Double] = []
    private var qualities: [Float] = []
    private var lostEvents = 0
    private var recoveries = 0
    private var fusedFrames = 0
    private var lostSince: Date?
    private var lastHandMask: [UInt8]?
    private(set) var turnedDegrees: Float = 0
    /// Underside views covered (coverage cells below the object), and when the flip cue was last shown.
    private(set) var undersideCells: Set<Int> = []
    private var flipCueAt: Date?
    private var lastObjectSpeed: Float = 0

    /// `seed`: the scan's in-box points at the start (object frame = world now). `planeY`: the table's height.
    init(box: ObjectBox, seed: [SIMD3<Float>], planeY: Float?, renderer: PointCloudRenderer?, diag: Diag, rawDir: URL?) {
        self.box = box
        self.diag = diag
        self.planeY = planeY
        self.rawDir = rawDir
        let args = CommandLine.arguments
        useSegmentation = HandMask.segmentationSupported && !args.contains("--handheld-no-segmentation")
        useHandPose = !useSegmentation || args.contains("--handheld-hand-pose")
        let tb = TrackBox(center: box.center, half: box.size / 2, yaw: box.yaw)
        tracker = ObjectTracker(box: tb, seed: seed)
        fusion = renderer.flatMap { HandheldFusion(device: $0.device) }
        var removed = 0
        if let renderer, let fusion {
            removed = fusion.prepare(renderer: renderer, box: box, planeY: planeY, clearance: TrackerConfig.standard.tableClearance)
            fusion.objectToWorld = { [weak self] t in MainActor.assumeIsolated { self?.objectToWorld(at: t) ?? Rigid.identity } }
            renderer.handheld = fusion
        }
        if let rawDir { try? RawFrames.writeSetup(dir: rawDir, box: tb, seed: seed) }
        diag.log("handheld_start", ["seed_points": seed.count, "model_points": tracker.model.count,
                                    "segmentation": useSegmentation, "hand_pose": useHandPose,
                                    "segmentation_supported": HandMask.segmentationSupported, "removed_points": removed,
                                    "box_cm": [box.size.x, box.size.y, box.size.z].map { ($0 * 1000).rounded() / 10 },
                                    "record_raw": rawDir != nil])
    }

    /// Person segmentation alongside scene depth, when the phone supports both (keeps world tracking running).
    func configure(_ session: ARSession) {
        guard useSegmentation, let config = session.configuration as? ARWorldTrackingConfiguration else { return }
        guard !config.frameSemantics.contains(.personSegmentationWithDepth) else { return }
        let c = config.copy() as! ARWorldTrackingConfiguration
        c.frameSemantics.insert(.personSegmentationWithDepth)
        session.run(c)
    }

    func stop() {
        logStats(final: true)
    }

    // MARK: Frames

    func feed(_ frame: ARFrame) {
        let t = frame.timestamp
        guard !busy, t - lastFed >= Self.interval * 0.9 else { return }
        let c0 = Date()
        guard let copy = Self.copy(frame, planeY: planeY, segmentation: useSegmentation) else { return }
        copyMS.append(Date().timeIntervalSince(c0) * 1000)
        lastFed = t
        busy = true
        frameIndex += 1
        let index = frameIndex
        let tracker = tracker
        let wantHandPose = useHandPose && index % 2 == 0
        let previousMask = lastHandMask
        let record = rawDir != nil && index % 3 == 0 && rawWritten < 600
        let rawDir = rawDir
        queue.async {
            var f = copy.frame
            var handMask = previousMask
            if wantHandPose {
                handMask = HandMask.fromHandPose(luma: f.luma, depth: f.depth, width: f.depthWidth, height: f.depthHeight, fx: f.depthK.x)
            }
            if f.personMask == nil, let handMask { f.personMask = handMask }
            let r = tracker.process(f)
            let rgba = r.fuse ? Self.rgba(copy, width: f.depthWidth, height: f.depthHeight) : []
            if record, let rawDir { try? RawFrames.write(f, result: r, index: index / 3 - 1, dir: rawDir) }
            let frame = f
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    self.busy = false
                    if wantHandPose { self.lastHandMask = handMask }
                    if record { self.rawWritten += 1 }
                    self.apply(r, frame: frame, rgba: rgba)
                }
            }
        }
    }

    private func apply(_ r: TrackResult, frame f: TrackerFrame, rgba: [UInt8]) {
        let previous = latest
        latest = r
        trackMS.append(r.ms)
        qualities.append(r.quality)
        if r.state != .lost {
            if let last = poses.last {
                let dt = Float(max(1e-3, r.timestamp - last.t))
                lastObjectSpeed = Rigid.angle(last.m, r.objectToWorld) / dt
            }
            poses.append((r.timestamp, r.objectToWorld))
            if poses.count > 3 { poses.removeFirst() }
            turnedDegrees = Rigid.angle(Rigid.identity, r.objectToWorld) * 180 / .pi
        }
        if r.state == .lost, previous?.state != .lost {
            lostEvents += 1
            lostSince = Date()
            poses = []
            diag.log("handheld_lost", ["quality": Double(r.quality), "since_start_s": Date().timeIntervalSince(started),
                                       "points": r.points, "features": r.featuresSeen])
        }
        if r.state == .recovered {
            recoveries += 1
            diag.log("handheld_recovered", ["after_s": lostSince.map { Date().timeIntervalSince($0) } ?? -1,
                                            "start": r.start, "quality": Double(r.quality)])
            lostSince = nil
        }
        if r.fuse, let fusion {
            fusedFrames += 1
            let cameraToObject = Rigid.inverse(r.objectToWorld) * f.cameraToWorld
            fusion.submit(.init(width: f.depthWidth, height: f.depthHeight, depth: f.depth, confidence: f.confidence,
                                rgba: rgba, mask: f.personMask ?? [UInt8](repeating: 0, count: f.depthWidth * f.depthHeight),
                                uniforms: .init(cameraToWorld: f.cameraToWorld, cameraToObject: cameraToObject,
                                                intrinsics: f.depthK,
                                                plane: SIMD4(planeY ?? 0, TrackerConfig.standard.tableClearance, planeY == nil ? 0 : 1, 0),
                                                params: SIMD4(1, UInt32(PointCloudRenderer.maxPoints), 1, 1500))))
        }
        if Date().timeIntervalSince(lastStats) >= 5 { logStats(final: false) }
    }

    // MARK: Poses for the scan

    var isLost: Bool { latest?.state == .lost }
    var quality: Float { latest?.quality ?? 1 }

    /// The object's pose at a frame time: the latest tracked pose continued at constant velocity (≤ 2 steps), frozen
    /// while lost. Identity until the first result (the object has not moved).
    func objectToWorld(at t: Double) -> simd_float4x4 {
        guard let b = poses.last else { return latest?.objectToWorld ?? Rigid.identity }
        guard poses.count >= 2, !isLost else { return b.m }
        let a = poses[poses.count - 2]
        return Rigid.extrapolate(a.m, at: a.t, b.m, at: b.t, to: t, pivot: Rigid.apply(b.m, box.center), maxStep: 2)
    }

    /// The camera relative to the object, in the object frame: what stills, keyframes and coverage use.
    func cameraInObject(_ cameraToWorld: simd_float4x4, at t: Double) -> simd_float4x4 {
        Rigid.inverse(objectToWorld(at: t)) * cameraToWorld
    }

    func toWorld(_ p: SIMD3<Float>, at t: Double) -> SIMD3<Float> { Rigid.apply(objectToWorld(at: t), p) }

    /// The stillness gate's extra condition: tracking well, a fresh result, and the object not turning fast (the
    /// camera-relative stillness itself comes from the poses `cameraInObject` gives the shared gate).
    func allowsStill(at t: Double) -> Bool {
        guard let r = latest, r.state == .tracking, r.quality >= Self.recordQuality, t - r.timestamp < 0.3 else { return false }
        return lastObjectSpeed < 10 * .pi / 180
    }

    /// Keyframes need a trusted pose too (they build the backend's mesh).
    func allowsKeyframe(at t: Double) -> Bool {
        guard let r = latest, r.state == .tracking, r.quality >= Self.recordQuality else { return false }
        return t - r.timestamp < 0.3
    }

    // MARK: Coverage

    /// The coverage cell of a camera position in the object frame, folded so the underside counts: views from below
    /// the object's middle fill the same ring (by angle below) on the dial, and are recorded as `b<ring>s<sector>`.
    func coverage(camera: SIMD3<Float>) -> (cell: Int, id: String)? {
        let d = camera - box.center
        let horizontal = (d.x * d.x + d.z * d.z).squareRoot()
        let e = atan2(d.y, horizontal) * 180 / .pi
        let below = e < 0
        let el = abs(e)
        let ring = CoverageDome.ringBounds.dropFirst().firstIndex { el < $0 }.map { $0 - 1 } ?? CoverageDome.rings - 1
        let cell = ring * CoverageDome.sectors + CoverageDome.sector(azimuth: CoverageDome.azimuth(d))
        if below, ring >= 1 {
            undersideCells.insert(cell)
            return (cell, String(format: "b%ds%02d", ring, cell % CoverageDome.sectors))
        }
        return (cell, CoverageDome.id(cell))
    }

    // MARK: Cues

    /// The hand-held cue, or nil to keep the scan's own. Generic words only.
    func cue(replacing current: String?, covered: Int) -> String? {
        if isLost { return "Hold it still for a moment" }
        let since = Date().timeIntervalSince(started)
        if since < 3, turnedDegrees < 10 { return "Hold it up and turn it slowly" }
        if lastObjectSpeed > 70 * .pi / 180 { return "Turn it more slowly" }
        if covered >= 10, undersideCells.isEmpty, since > 8, flipCueAt.map({ Date().timeIntervalSince($0) > 20 }) ?? true {
            flipCueAt = Date()
        }
        if let at = flipCueAt, Date().timeIntervalSince(at) < 4, undersideCells.isEmpty { return "Now flip it over" }
        if current == "Keep walking around it" { return "Keep turning it slowly" }
        return nil
    }

    // MARK: Stats

    private func logStats(final: Bool) {
        lastStats = Date()
        func pct<T: Comparable>(_ v: [T], _ p: Double) -> T? { v.isEmpty ? nil : v.sorted()[Int(Double(v.count - 1) * p)] }
        var fields: [String: Any] = ["final": final, "frames": trackMS.count, "lost": lostEvents, "recovered": recoveries,
                                     "fused": fusedFrames, "turned_deg": Int(turnedDegrees), "underside_cells": undersideCells.count,
                                     "model_points": latest?.modelPoints ?? 0, "features": latest?.bankFeatures ?? 0,
                                     "gpu_fused": fusion?.fused ?? 0, "state": latest?.state.rawValue ?? "none",
                                     "raw_frames": rawWritten]
        if let v = pct(trackMS, 0.5) { fields["track_ms_p50"] = (v * 10).rounded() / 10 }
        if let v = pct(trackMS, 0.95) { fields["track_ms_p95"] = (v * 10).rounded() / 10 }
        if let v = pct(copyMS, 0.95) { fields["copy_ms_p95"] = (v * 100).rounded() / 100 }
        if let v = pct(qualities, 0.5) { fields["quality_p50"] = (Double(v) * 100).rounded() / 100 }
        if let v = pct(qualities, 0.1) { fields["quality_p10"] = (Double(v) * 100).rounded() / 100 }
        diag.log("handheld_stats", fields)
        trackMS = []
        copyMS = []
        qualities = []
    }

    // MARK: Copying a frame (main queue)

    struct Copy: Sendable {
        var frame: TrackerFrame
        /// The chroma plane (interleaved Cb, Cr) sampled at the depth map's size, for colouring the fused points.
        var chroma: [UInt8]
        var chromaWidth: Int, chromaHeight: Int
    }

    static let lumaStep = 3

    static func copy(_ frame: ARFrame, planeY: Float?, segmentation: Bool) -> Copy? {
        guard let depth = DepthCopy.from(frame) else { return nil }
        let img = frame.capturedImage
        CVPixelBufferLockBaseAddress(img, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(img, .readOnly) }
        guard CVPixelBufferGetPlaneCount(img) >= 2, let yb = CVPixelBufferGetBaseAddressOfPlane(img, 0),
              let cb = CVPixelBufferGetBaseAddressOfPlane(img, 1) else { return nil }
        let iw = CVPixelBufferGetWidthOfPlane(img, 0), ih = CVPixelBufferGetHeightOfPlane(img, 0)
        let yr = CVPixelBufferGetBytesPerRowOfPlane(img, 0), cr = CVPixelBufferGetBytesPerRowOfPlane(img, 1)
        let s = lumaStep
        let lw = iw / s, lh = ih / s
        var luma = [UInt8](repeating: 0, count: lw * lh)
        let yp = yb.assumingMemoryBound(to: UInt8.self), cp = cb.assumingMemoryBound(to: UInt8.self)
        luma.withUnsafeMutableBufferPointer { out in
            for y in 0..<lh {
                let row = yp + y * s * yr
                for x in 0..<lw { out[y * lw + x] = row[x * s] }
            }
        }
        // Chroma (half the image size per side) sampled at the depth map's size: enough for point colours.
        let cw = depth.width, ch = depth.height
        let ciw = CVPixelBufferGetWidthOfPlane(img, 1), cih = CVPixelBufferGetHeightOfPlane(img, 1)
        var chroma = [UInt8](repeating: 128, count: cw * ch * 2)
        chroma.withUnsafeMutableBufferPointer { out in
            for y in 0..<ch {
                let row = cp + min(cih - 1, y * cih / ch) * cr
                for x in 0..<cw {
                    let sx = min(ciw - 1, x * ciw / cw) * 2
                    out[(y * cw + x) * 2] = row[sx]
                    out[(y * cw + x) * 2 + 1] = row[sx + 1]
                }
            }
        }
        let k = frame.camera.intrinsics
        let ls = 1 / Float(s)
        let f = TrackerFrame(timestamp: frame.timestamp, cameraToWorld: frame.camera.transform, depth: depth.depth,
                             confidence: depth.confidence, depthWidth: depth.width, depthHeight: depth.height,
                             depthK: SIMD4(depth.fx, depth.fy, depth.cx, depth.cy),
                             luma: GrayImage(width: lw, height: lh, pixels: luma),
                             lumaK: SIMD4(k[0][0] * ls, k[1][1] * ls, k[2][0] * ls, k[2][1] * ls),
                             personMask: segmentation ? HandMask.fromSegmentation(frame, width: depth.width, height: depth.height) : nil,
                             planeY: planeY)
        return Copy(frame: f, chroma: chroma, chromaWidth: cw, chromaHeight: ch)
    }

    /// RGBA at depth resolution from the grey copy + chroma (BT.601 full range, as the renderer's shader).
    nonisolated static func rgba(_ c: Copy, width w: Int, height h: Int) -> [UInt8] {
        var out = [UInt8](repeating: 255, count: w * h * 4)
        let l = c.frame.luma
        for y in 0..<h {
            let ly = min(l.height - 1, y * l.height / h)
            for x in 0..<w {
                let lx = min(l.width - 1, x * l.width / w)
                let Y = Float(l.pixels[ly * l.width + lx]) / 255
                let i = (y * c.chromaWidth + x) * 2
                let cb = Float(c.chroma[i]) / 255 - 0.5, cr = Float(c.chroma[i + 1]) / 255 - 0.5
                let r = Y + 1.402 * cr, g = Y - 0.7141 * cr - 0.3441 * cb, b = Y + 1.772 * cb
                let o = (y * w + x) * 4
                out[o] = UInt8(max(0, min(255, r * 255)))
                out[o + 1] = UInt8(max(0, min(255, g * 255)))
                out[o + 2] = UInt8(max(0, min(255, b * 255)))
            }
        }
        return out
    }
}
