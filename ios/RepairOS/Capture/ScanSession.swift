import ARKit
import CoreImage
import Foundation
import Observation

/// The scanner, as simple as the person needs it: point at the object, tap Scan, walk around while the live LiDAR
/// point cloud fills in, tap Done. The object under the screen centre is locked automatically (the cloud brightens
/// on it and dims elsewhere; a tap only corrects it). Photos, torch and upload are automatic.
///
/// Under the hood (PointCloudRenderer draws): a 12 MP still every ~10 cm or 15° of movement while the object is in
/// view and the frame is sharp, each with pose, intrinsics, exposure, ISO, the nearest frame's depth + confidence
/// (and the gap) and the torch level; and at ~5 Hz throughout, depth + confidence + a 640 px RGB frame + pose +
/// intrinsics as scan keyframes. Every record carries the segment id. Layout: ios/README.md "Capture layout".
///
/// Threading: the AR delegate runs on the main queue and only copies small buffers at ≤ 10 Hz; files are written
/// on a background queue; ARFrames are never kept. Observable state changes ≤ 10 Hz.
@MainActor
@Observable
final class ScanSession: NSObject {
    enum Stage: Equatable { case ready, locking, scanning, finishing, failed(String) }

    // UI state (≤ 10 Hz).
    private(set) var stage: Stage = .ready
    private(set) var cue: String?
    private(set) var stills: [String] = []
    private(set) var shotTick = 0
    private(set) var lockTick = 0
    private(set) var capturing = false
    private(set) var torchLevel: Float = 0
    private(set) var batteryQuestion = false
    private(set) var boxPoints = 0
    private(set) var box: ObjectBox?
    /// 0...1 for the thin ring: half the in-box points goal, half the stills goal.
    var progress: Double {
        min(1, Double(boxPoints) / Double(Self.pointsGoal)) * 0.5 + min(1, Double(stills.count) / Double(Self.stillsGoal)) * 0.5
    }

    let record: SessionRecord
    let storage: ScanStorage
    let upload: UploadQueue?
    let diag: Diag
    let hasLiDAR: Bool
    let session = ARSession()
    private(set) var renderer: PointCloudRenderer?
    private(set) var torch: Torch
    private let analyzer = FrameAnalyzer()
    private let recorder: FrameRecorder
    /// 0 = the object as it stands; the flip step (later) starts 1.
    private(set) var segment = 0

    private var lastTick: TimeInterval = 0
    private var lastRecord: TimeInterval = 0
    private var lastKeyframePose: simd_float4x4?
    private var lastHeartbeat = Date()
    private var lastStillPose: simd_float4x4?
    private var lastPose: (t: TimeInterval, m: simd_float4x4)?
    private var angularSpeed: Float = 0
    private var analyzing = false
    private var lastResult: QualityResult?
    private var videoFormat: ARConfiguration.VideoFormat?
    private var probe: StillProbeResult?
    private var rejects: [String: Int] = [:]
    private var started = Date()
    private var scanTapped: Date?
    private var lockedAt: Date?
    private var firstPointsAt: Date?
    private var planeAnchorID: UUID?
    private var viewSize: CGSize = .zero
    private var lockAttempts = 0
    private let exposure: ExposureControl
    /// Per-frame camera poses over the last ~0.3 s, for the "nearly still" gate.
    private var recentPoses: [(t: TimeInterval, m: simd_float4x4)] = []
    private var centerDepth: Float?
    // Stills are few (≈ 15 per scan): the gate starts after 3 of them.
    private var orbitSharpness = RunningMedian(capacity: 30, minCount: 3)
    private var closeSharpness = RunningMedian(capacity: 30, minCount: 3)
    private var lastCloseShotPose: simd_float4x4?
    private var closeStills = 0
    private var stillsRejected = 0

    static let tickInterval: TimeInterval = 0.1
    static let recordInterval: TimeInterval = 0.2
    nonisolated static let stillMove: Float = 0.12
    nonisolated static let stillTurn: Float = 18 * .pi / 180
    /// Depth keyframes only when the phone has moved: standing still adds nothing to the Mac's mesh.
    nonisolated static let keyframeMove: Float = 0.03
    nonisolated static let keyframeTurn: Float = 5 * .pi / 180
    static let pointsGoal = 20_000
    static let stillsGoal = 12
    static let autoLockTries = 8
    /// "Nearly still" over the last ~150 ms (fast shutter + no motion = sharp stills).
    static let stillWindow: TimeInterval = 0.15
    static let maxStillLinear: Float = 0.06            // m/s
    static let maxStillAngular: Float = 8 * .pi / 180  // rad/s
    /// A 12 MP still below this share of the session's running median sharpness is dropped.
    nonisolated static let stillSharpnessRatio = 0.7
    /// Close-ups: when the view's centre shows much more fine detail than usual, ask to get close and shoot there.
    static let closeRange: ClosedRange<Float> = 0.15...0.25
    /// Small or flat objects (under 3 cm tall or 15 cm across) get closer close-ups.
    static let closeRangeSmall: ClosedRange<Float> = 0.12...0.20
    var closeBand: ClosedRange<Float> {
        guard let box else { return Self.closeRange }
        return box.size.y < 0.03 || max(box.size.x, box.size.z) < 0.15 ? Self.closeRangeSmall : Self.closeRange
    }
    static let detailFactor = 1.6
    nonisolated static let closeMove: Float = 0.04
    nonisolated static let closeTurn: Float = 8 * .pi / 180
    static let maxCloseStills = 10

    // "Continue scanning": the same scan, a new segment, relocalised into the saved world map when possible.
    let continuing: Bool
    private var previous: ScanPackage?
    private var stillOffset = 0
    /// Relocalising against the saved map (continuation only); `aligned` once it matched.
    private(set) var relocalizing = false
    private(set) var aligned = true
    private var scanWanted = false
    static let relocalizeTimeout: TimeInterval = 5

    init(record: SessionRecord, storage: ScanStorage, upload: UploadQueue?, probe: StillProbeResult?, continuing: Bool = false) {
        self.continuing = continuing
        self.record = record
        self.storage = storage
        self.upload = upload
        self.probe = probe
        diag = Diag.session(storage.dir(record))
        hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        torch = Torch(diag: diag)
        exposure = ExposureControl(diag: Diag.session(storage.dir(record)))
        recorder = FrameRecorder(scanDir: storage.scanDir(record), prefix: "scans/\(record.scanID)")
        super.init()
        renderer = PointCloudRenderer(session: session)
        recorder.onWritten = { [weak self] kf in
            self?.upload?.sendAhead([kf.imageFile, kf.depthFile, kf.confidenceFile].compactMap { $0 })
        }
        if continuing {
            previous = (try? Data(contentsOf: storage.scanPackageURL(record))).flatMap { try? JSON.decoder.decode(ScanPackage.self, from: $0) }
            segment = record.segmentCount ?? 1
            stillOffset = record.stillIDs.count
            recorder.startIndex(previous?.keyframes.count ?? 0)
        }
    }

    // MARK: - Session

    func start() {
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        config.planeDetection = [.horizontal]
        config.isAutoFocusEnabled = true
        if hasLiDAR {
            config.frameSemantics.insert(.sceneDepth)
            if ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth) {
                config.frameSemantics.insert(.smoothedSceneDepth)
            }
        }
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
        videoFormat = probe?.chosenFormat.flatMap { key in formats.first { $0.key == key } }
            ?? ARConfiguration.VideoFormat.stillCandidates().first
        if let videoFormat { config.videoFormat = videoFormat }
        session.delegate = self
        if continuing, let map = Self.loadWorldMap(storage.dir(record)) {
            // Back into the scan's world frame: same box, same cloud, poses comparable with the first visit.
            config.initialWorldMap = map
            relocalizing = true
            aligned = false
            cue = "Point at the object to pick up where you left off"
            session.run(config, options: [.resetTracking, .removeExistingAnchors])
            let fresh = config.copy() as! ARWorldTrackingConfiguration
            fresh.initialWorldMap = nil
            Task {
                try? await Task.sleep(for: .seconds(Self.relocalizeTimeout))
                guard self.relocalizing else { return }
                // Not recognised in time: carry on in a frame of its own (aligned false; the Mac registers it).
                self.relocalizing = false
                self.session.run(fresh, options: [.resetTracking, .removeExistingAnchors])
                self.cue = nil
                self.diag.log("relocalize", ["ok": false, "after_s": Self.relocalizeTimeout, "segment": self.segment])
                if self.scanWanted { self.autoLock() }
            }
        } else {
            if continuing { aligned = false }
            session.run(config)
        }
        // Fast shutter once ARKit has its camera running (setting it before the first frames is not kept).
        Task {
            try? await Task.sleep(for: .seconds(1))
            self.exposure.start()
        }
        CaptureRegistry.active = record.id
        started = Date()
        diag.log("scan_start", ["continuing": continuing, "segment": segment, "lidar": hasLiDAR, "format": videoFormat?.label ?? "default", "renderer": renderer != nil,
                                "model": DeviceCapabilities.current().model, "torch_mode": torch.mode.rawValue])
    }

    func stop() {
        exposure.stop()
        torch.off()
        session.pause()
        if CaptureRegistry.active == record.id { CaptureRegistry.active = nil }
    }

    func setViewSize(_ s: CGSize) { viewSize = s }

    // MARK: - Scan + lock

    /// "Scan": lock the object under the screen centre (retrying for ~2 s while the cloud fills in), then shoot.
    func beginScan() {
        guard stage == .ready else { return }
        scanTapped = Date()
        lockAttempts = 0
        diag.log("scan_tapped", ["since_start_s": Int(Date().timeIntervalSince(started))])
        if relocalizing {
            // Still matching the saved map: start as soon as it matches (or gives up).
            scanWanted = true
            stage = .locking
            cue = "Finding where you left off…"
            return
        }
        if continuing, aligned, box != nil {
            stage = .scanning
            lockedAt = Date()
            return
        }
        autoLock()
    }

    static func loadWorldMap(_ dir: URL) -> ARWorldMap? {
        guard let data = try? Data(contentsOf: dir.appending(path: "world.arworldmap")) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: data)
    }

    /// The saved map matched: reuse the scan's box and show its cloud, so new points refine it.
    private func relocalized() {
        relocalizing = false
        aligned = true
        if let cb = previous?.cropBox {
            let q = simd_quatf(ix: Float(cb.quaternionXyzw[0]), iy: Float(cb.quaternionXyzw[1]),
                               iz: Float(cb.quaternionXyzw[2]), r: Float(cb.quaternionXyzw[3]))
            box = ObjectBox(center: SIMD3(cb.centerM.map(Float.init)), size: SIMD3(cb.extentM.map(Float.init)),
                            yaw: 2 * atan2(q.imag.y, q.real))
            renderer?.box = box
        }
        var seeded = 0
        if let file = previous?.pointsFile, let pts = ScanCloudReader.read(storage.dir(record).appending(path: file)) {
            seeded = renderer?.seed(pts) ?? 0
        }
        cue = nil
        diag.log("relocalize", ["ok": true, "after_s": Int(Date().timeIntervalSince(started)), "seeded_points": seeded,
                                "segment": segment])
        if scanWanted {
            scanWanted = false
            stage = .ready
            beginScan()     // relocalised with a box → straight to scanning; without one → the usual auto-lock
        }
    }

    private func autoLock() {
        guard stage == .ready || stage == .locking, viewSize != .zero else { return }
        lockAttempts += 1
        lock(at: CGPoint(x: viewSize.width / 2, y: viewSize.height / 2), auto: true)
    }

    /// A tap corrects the lock (any time).
    func tap(at point: CGPoint) {
        guard stage != .finishing else { return }
        lock(at: point, auto: false)
    }

    private func lock(at point: CGPoint, auto: Bool) {
        guard let frame = session.currentFrame, let renderer, viewSize != .zero else { return }
        let t0 = Date()
        if box == nil { stage = .locking }
        // The view point as a ray in world space (portrait view → normalized image → camera ray).
        let inv = frame.displayTransform(for: .portrait, viewportSize: viewSize).inverted()
        let n = CGPoint(x: point.x / viewSize.width, y: point.y / viewSize.height).applying(inv)
        let res = frame.camera.imageResolution
        let k = frame.camera.intrinsics
        let u = Float(n.x * res.width), v = Float(n.y * res.height)
        let dirCam = simd_normalize(SIMD3((u - k[2][0]) / k[0][0], -(v - k[2][1]) / k[1][1], -1))
        let m = frame.camera.transform
        let origin = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let d4 = m * SIMD4(dirCam, 0)
        let dir = SIMD3(d4.x, d4.y, d4.z)
        let query = ARRaycastQuery(origin: origin, direction: dir, allowing: .existingPlaneGeometry, alignment: .horizontal)
        let estimated = ARRaycastQuery(origin: origin, direction: dir, allowing: .estimatedPlane, alignment: .horizontal)
        guard let hit = session.raycast(query).first ?? session.raycast(estimated).first else {
            return lockFailed("no support plane under the \(auto ? "centre" : "tap")", t0, auto: auto)
        }
        let hitPoint = SIMD3(hit.worldTransform.columns.3.x, hit.worldTransform.columns.3.y, hit.worldTransform.columns.3.z)
        let plane = hit.anchor as? ARPlaneAnchor
        let planeY = plane.map { $0.transform.columns.3.y } ?? hitPoint.y
        let anchorID = plane?.identifier
        let cloud = renderer.points()
        let pts = cloud.map(\.0), cols = cloud.map(\.1)
        Task.detached(priority: .userInitiated) {
            let fitted = BoxFitter.fit(points: pts, colors: cols, tap: hitPoint, planeY: planeY, rayOrigin: origin,
                                       rayDirection: dir, refinePlane: anchorID == nil)
            let used = fitted.map { b in pts.filter { b.contains($0) }.count } ?? 0
            await self.locked(fitted, t0: t0, points: pts.count, used: used, auto: auto, anchor: anchorID)
        }
    }

    private func lockFailed(_ why: String, _ t0: Date, auto: Bool) {
        diag.log("lock_failed", ["segment": segment, "auto": auto, "attempt": lockAttempts, "reason": why,
                                 "lock_ms": Int(Date().timeIntervalSince(t0) * 1000)])
        guard box == nil else { return }
        if auto, lockAttempts < Self.autoLockTries {
            Task {
                try? await Task.sleep(for: .milliseconds(250))
                self.autoLock()
            }
            return
        }
        // Never stuck: scan the whole view and keep shooting; a tap can still lock the object.
        stage = .scanning
        cue = "Tap the object to focus on it"
    }

    private func locked(_ fitted: ObjectBox?, t0: Date, points n: Int, used: Int, auto: Bool, anchor: UUID?) {
        guard let fitted else { return lockFailed("no object cluster (\(n) cloud points)", t0, auto: auto) }
        box = fitted
        planeAnchorID = anchor ?? planeAnchorID
        renderer?.box = fitted
        if lockedAt == nil { lockedAt = Date() }
        stage = .scanning
        cue = nil
        lockTick += 1
        diag.log("lock", ["segment": segment, "auto": auto, "attempt": lockAttempts, "lock_ms": Int(Date().timeIntervalSince(t0) * 1000),
                          "since_scan_tap_ms": scanTapped.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1,
                          "cloud_points": n, "points_in_box": used, "plane_anchor": anchor != nil,
                          "size_cm": [fitted.size.x, fitted.size.y, fitted.size.z].map { ($0 * 1000).rounded() / 10 }])
    }

    // MARK: - Done

    /// Writes the in-box cloud, the scan package (with every recorded frame) and the world map, then queues the
    /// upload. Returns the updated record.
    func finish() async -> SessionRecord {
        stage = .finishing
        upload?.markDone()
        exposure.stop()
        torch.off()
        var keyframes = await recorder.drain()
        // A continuation that did not relocalise lives in another frame: keep the first visit's map.
        if !continuing || aligned { await saveWorldMap() }
        var plane = supportPlane()
        session.pause()
        if CaptureRegistry.active == record.id { CaptureRegistry.active = nil }
        var r = record
        do {
            var pointsFile: String?, count: Int?
            var segments = previous?.segments ?? []
            var cropBox = box?.contract
            // An unaligned continuation writes its own cloud; everything else is the merged in-box cloud.
            let ownCloud = continuing && !aligned
            if let renderer, hasLiDAR {
                let pts = renderer.points(in: box, margin: 0.005)
                if !pts.isEmpty {
                    let name = ownCloud ? "points_segment\(segment)_arkit_world.ply" : "points_arkit_world.ply"
                    let url = storage.scanDir(record).appending(path: name)
                    count = try PLYWriter.write(pts.map(\.0), colors: pts.map(\.1), to: url)
                    pointsFile = "scans/\(record.scanID)/\(url.lastPathComponent)"
                }
            }
            if continuing, let prev = previous {
                keyframes = prev.keyframes + keyframes
                plane = prev.supportPlane ?? plane
                cropBox = prev.cropBox ?? cropBox
                segments.append(Segment_(aligned: aligned ? nil : false, cropBox: box?.contract, pointCount: ownCloud ? count : nil,
                                         pointsFile: ownCloud ? pointsFile : nil, reason: .continued, segment: segment))
                if ownCloud || pointsFile == nil {
                    pointsFile = prev.pointsFile
                    count = prev.pointCount
                }
            }
            // No voxel_mm: the cloud is not voxel-fused; every LiDAR sample (every 2nd depth pixel) is kept.
            let pkg = ScanPackage(
                alignment: nil, capturedAt: previous?.capturedAt ?? started, coverageHint: nil, cropBox: cropBox,
                depthSource: pointsFile == nil ? DepthSource.none : .lidar, device: DeviceCapabilities.current().contract,
                frame: .arkitWorld, gravityArkitWorld: [0, -1, 0], keyframes: keyframes, meshFile: nil, pointCount: count,
                pointsFile: pointsFile, poseSource: .arkit, productID: record.productID,
                scanID: record.scanID, segments: segments.isEmpty ? nil : segments, sessionID: record.id, supportPlane: plane,
                supportingPlaneRemoved: box != nil ? true : nil, symptom: record.symptom, units: .m,
                voxelMm: nil)
            try JSON.encoder.encode(pkg).write(to: storage.scanPackageURL(record), options: .atomic)
            r.stillIDs = record.stillIDs + stills
            r.status = .captured
            if continuing { r.segmentCount = segment + 1 }
            try? storage.save(r)
            if continuing {
                // Same scan_id: the new stills, then the scan package again (the backend rebuilds the report).
                upload?.enqueueAll(stillIDs: stills, scanReady: false)
                upload?.repostScan()
            } else {
                upload?.enqueueAll(stillIDs: stills, scanReady: true)
            }
            diag.log("finish", ["continuing": continuing, "segment": segment, "aligned": aligned,
                                "stills_rejected": stillsRejected, "close_stills": closeStills,
                                "orbit_sharpness": orbitSharpness.values.map { ($0 * 10).rounded() / 10 },
                                "close_sharpness": closeSharpness.values.map { ($0 * 10).rounded() / 10 },
                                "exposure": ["duration_s": exposure.duration, "iso": exposure.iso, "active": exposure.active],
                                "stills": stills.count, "keyframes": keyframes.count, "points_in_box": count ?? 0,
                                "support_plane": plane != nil, "locked": box != nil,
                                "total_s": Int(Date().timeIntervalSince(started)),
                                "scan_s": scanTapped.map { Int(Date().timeIntervalSince($0)) } ?? -1,
                                "rejects": rejects, "frame_ms": frameStats()])
        } catch {
            diag.log("error", ["where": "finish", "message": error.localizedDescription])
            stage = .failed("Could not save the scan: \(error.localizedDescription)")
        }
        return r
    }

    private func supportPlane() -> SupportPlane? {
        guard let anchors = session.currentFrame?.anchors else { return nil }
        let planes = anchors.compactMap { $0 as? ARPlaneAnchor }
        guard let plane = planes.first(where: { $0.identifier == planeAnchorID }) ?? planes.first(where: { $0.alignment == .horizontal })
        else { return nil }
        let t = plane.transform
        let rowMajor = (0..<4).flatMap { r in (0..<4).map { c in Double(t[c][r]) } }
        return SupportPlane(extentM: [Double(plane.planeExtent.width), Double(plane.planeExtent.height)], transform: rowMajor)
    }

    /// Phase C (AR pins on the real object) relocalises against this map.
    private func saveWorldMap() async {
        do {
            let map = try await session.currentWorldMap()
            let data = try NSKeyedArchiver.archivedData(withRootObject: map, requiringSecureCoding: true)
            try data.write(to: storage.dir(record).appending(path: "world.arworldmap"), options: .atomic)
            diag.log("world_map_saved", ["kb": data.count / 1024, "anchors": map.anchors.count])
        } catch {
            diag.log("world_map_failed", ["error": error.localizedDescription])
        }
    }

    func setTorchMode(_ m: TorchRule.Mode) { torch.setMode(m) }
    func answerBattery(_ yes: Bool) {
        torch.answerBattery(yes)
        batteryQuestion = false
    }

    // MARK: - Per frame (main queue, ≤ 10 Hz)

    fileprivate func handle(_ frame: ARFrame) {
        let t = frame.timestamp
        recentPoses.append((t, frame.camera.transform))
        if let first = recentPoses.first, t - first.t > 0.3 { recentPoses.removeFirst() }
        if hasLiDAR, stage == .scanning || stage == .locking, t - lastRecord >= Self.recordInterval,
           lastKeyframePose.map({ Self.moved(from: $0, to: frame.camera.transform, move: Self.keyframeMove, turn: Self.keyframeTurn) }) ?? true {
            lastRecord = t
            lastKeyframePose = frame.camera.transform
            recorder.record(frame, segment: segment)
        }
        guard t - lastTick >= Self.tickInterval else { return }
        lastTick = t
        let m = frame.camera.transform
        if relocalizing, case .normal = frame.camera.trackingState { relocalized() }
        centerDepth = Self.centerDepth(frame)
        exposure.update(time: t)
        if let last = lastPose, t > last.t {
            let fa = -SIMD3<Float>(last.m.columns.2.x, last.m.columns.2.y, last.m.columns.2.z)
            let fb = -SIMD3<Float>(m.columns.2.x, m.columns.2.y, m.columns.2.z)
            angularSpeed = 0.5 * angularSpeed + 0.5 * acos(max(-1, min(1, simd_dot(simd_normalize(fa), simd_normalize(fb))))) / Float(t - last.t)
        }
        lastPose = (t, m)
        if firstPointsAt == nil, (renderer?.appended ?? 0) > 0 {
            firstPointsAt = Date()
            diag.log("first_points", ["after_start_ms": Int(Date().timeIntervalSince(started) * 1000)])
        }
        heartbeat()
        guard !analyzing, let input = FrameAnalyzer.Input.make(frame: frame, box: box, depth: nil) else { return }
        analyzing = true
        let tracking: Bool = { if case .normal = frame.camera.trackingState { return true } else { return false } }()
        let limitedForLight: Bool = {
            if case .limited(.insufficientFeatures) = frame.camera.trackingState { return true } else { return false }
        }()
        let speed = angularSpeed
        Task {
            let out = await analyzer.analyze(input)
            self.analyzing = false
            self.apply(out, time: t, cameraToWorld: m, tracking: tracking, limitedForLight: limitedForLight,
                       angularSpeed: speed, projection: input.projection)
        }
    }

    private func apply(_ a: FrameAnalyzer.Output, time t: TimeInterval, cameraToWorld m: simd_float4x4, tracking: Bool,
                       limitedForLight: Bool, angularSpeed: Float, projection: Projection) {
        torch.update(time: t, ambient: a.ambient, meanLuma: a.meanLuma, saturated: a.saturated, isoHigh: exposure.isoHigh)
        torchLevel = torch.applied
        if torch.needsBatteryConsent, !batteryQuestion { batteryQuestion = true }
        guard stage == .scanning else {
            if stage == .ready { cue = a.meanLuma < QualityGate.darkLuma ? "Too dark: add light" : nil }
            return
        }
        // Framed: the box centre or ≥ 30 % of its visible area in the middle of the view, and the centre ray on it
        // (anything counts before a lock).
        let framing = box.map { FramingGate.evaluate(box: $0, cameraToWorld: m, projection: projection, centerDepth: centerDepth) }
        let inView = framing?.orbit ?? true
        let cam = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let metrics = QualityMetrics(sharpness: a.sharpness, medianSharpness: a.medianSharpness, meanLuma: a.meanLuma,
                                     ambient: a.ambient, distance: box.map { simd_length(cam - $0.center) }, band: 0.05...3,
                                     fill: 1, inFrame: inView, trackingNormal: tracking, angularSpeed: angularSpeed, minFill: 0)
        var result = QualityGate.evaluate(metrics)
        if limitedForLight, result.accept { result.reason = "Add light or move closer" }
        lastResult = result
        if !result.accept, let r = result.reason { rejects[r, default: 0] += 1 }
        let still = isStill(now: t)
        // Fine detail in the middle of the view, inside the object: get close there.
        let onObject = framing.map { $0.centerHitsBox ?? a.centerInObject } ?? false
        let detailHere = onObject && (a.centerDetailMedian.map { a.centerDetail > $0 * Self.detailFactor } ?? false)
        // Distance to the object along the view: LiDAR at the centre, else where the view ray enters the box (LiDAR
        // gives little depth nearer than ~20 cm).
        let viewDistance = centerDepth ?? box.flatMap { Self.rayBoxDistance(cameraToWorld: m, box: $0) }
        let band = closeBand
        let near = viewDistance.map { band.contains($0) } ?? false
        let wantClose = detailHere && closeStills < Self.maxCloseStills
        let moved = lastStillPose.map { Self.moved(from: $0, to: m) } ?? true
        let movedClose = lastCloseShotPose.map { Self.moved(from: $0, to: m, move: Self.closeMove, turn: Self.closeTurn) } ?? true
        if box != nil || cue == nil || cue == lastResult?.reason {
            // The detail rule's cue wins over everything else: getting close is the fix for most of the rejects too.
            if wantClose, !near, (viewDistance ?? 1) > band.upperBound {
                cue = "Move closer here"
            } else if !result.accept {
                cue = result.reason
            } else if (moved || (wantClose && near && movedClose)) && !still {
                cue = "Hold still a moment"
            } else {
                cue = result.reason ?? (moved ? nil : "Keep walking around it")
            }
        }
        // Close-ups need only the centre ray on the object (the box is far bigger than the view up close).
        let closeOK = framing?.close ?? false
        if wantClose, near, movedClose, closeOK, still, !capturing,
           result.accept || result.reason == "Keep the object in frame" {
            lastCloseShotPose = m
            Task { await fire(auto: true, lowLight: result.lowLight, close: true, gate: (a.sharpness, a.medianSharpness)) }
            return
        }
        guard result.accept, still, !capturing else { return }
        if moved {
            lastStillPose = m
            Task { await fire(auto: true, lowLight: result.lowLight, gate: (a.sharpness, a.medianSharpness)) }
        }
    }

    /// Distance along the camera's view ray (−Z) to where it enters the box, nil when it misses.
    nonisolated static func rayBoxDistance(cameraToWorld m: simd_float4x4, box: ObjectBox) -> Float? {
        let inv = simd_quatf(angle: -box.yaw, axis: SIMD3(0, 1, 0))
        let o = inv.act(SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z) - box.center)
        let d = inv.act(-SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z))
        let h = box.size / 2
        var tmin: Float = 0, tmax: Float = .greatestFiniteMagnitude
        for i in 0..<3 {
            if abs(d[i]) < 1e-6 {
                if abs(o[i]) > h[i] { return nil }
            } else {
                let t1 = (-h[i] - o[i]) / d[i], t2 = (h[i] - o[i]) / d[i]
                tmin = max(tmin, min(t1, t2))
                tmax = min(tmax, max(t1, t2))
            }
        }
        return tmin <= tmax ? tmin : nil
    }

    /// Nearly still: camera speed over the last ~150 ms below the limits.
    private func isStill(now t: TimeInterval) -> Bool {
        guard let last = recentPoses.last, let then = recentPoses.last(where: { last.t - $0.t >= Self.stillWindow }) else { return false }
        let dt = Float(last.t - then.t)
        let a = last.m, b = then.m
        let lin = simd_distance(SIMD3(a.columns.3.x, a.columns.3.y, a.columns.3.z), SIMD3(b.columns.3.x, b.columns.3.y, b.columns.3.z)) / dt
        let fa = -SIMD3(a.columns.2.x, a.columns.2.y, a.columns.2.z), fb = -SIMD3(b.columns.2.x, b.columns.2.y, b.columns.2.z)
        let ang = acos(max(-1, min(1, simd_dot(simd_normalize(fa), simd_normalize(fb))))) / dt
        return lin < Self.maxStillLinear && ang < Self.maxStillAngular
    }

    /// Median LiDAR depth in the centre of the depth map (nil without LiDAR or depth there).
    static func centerDepth(_ frame: ARFrame) -> Float? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), row = CVPixelBufferGetBytesPerRow(map)
        var v: [Float] = []
        for y in (h / 2 - 3)...(h / 2 + 3) {
            let r = base.advanced(by: y * row).assumingMemoryBound(to: Float32.self)
            for x in (w / 2 - 3)...(w / 2 + 3) where r[x].isFinite && r[x] > 0 { v.append(r[x]) }
        }
        return v.isEmpty ? nil : v.sorted()[v.count / 2]
    }

    /// Sharpness of a 12 MP still where it matters: Laplacian variance of the full-resolution luma in the centre
    /// (768 × 768), adjacent pixels, so fine motion blur shows.
    nonisolated static func centerSharpness(_ image: CVPixelBuffer, side: Int = 768) -> Double {
        CVPixelBufferLockBaseAddress(image, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(image, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(image, 0) else { return 0 }
        let w = CVPixelBufferGetWidthOfPlane(image, 0), h = CVPixelBufferGetHeightOfPlane(image, 0)
        let row = CVPixelBufferGetBytesPerRowOfPlane(image, 0)
        let s = min(side, w - 2, h - 2), x0 = (w - s) / 2, y0 = (h - s) / 2
        let p = base.assumingMemoryBound(to: UInt8.self)
        var sum = 0.0, sumSq = 0.0
        for y in (y0 + 1)..<(y0 + s - 1) {
            let r = y * row
            for x in (x0 + 1)..<(x0 + s - 1) {
                let lap = Double(p[r - row + x]) + Double(p[r + row + x]) + Double(p[r + x - 1]) + Double(p[r + x + 1]) - 4 * Double(p[r + x])
                sum += lap
                sumSq += lap * lap
            }
        }
        let n = Double((s - 2) * (s - 2))
        return max(0, sumSq / n - (sum / n) * (sum / n))
    }

    private func heartbeat() {
        guard Date().timeIntervalSince(lastHeartbeat) >= 5 else { return }
        lastHeartbeat = Date()
        let inBox = box.map { b in renderer?.points(in: b).count ?? 0 } ?? 0
        boxPoints = inBox
        diag.log("heartbeat", ["stage": "\(stage)", "segment": segment, "cloud_appended": renderer?.appended ?? 0,
                               "points_in_box": inBox, "stills": stills.count, "keyframes": recorder.count,
                               "frame_ms": frameStats(), "torch": torch.applied,
                               "tracking": "\(session.currentFrame?.camera.trackingState ?? .notAvailable)"])
    }

    /// Mean and 95th-percentile display frame time over the last ~10 s (60 fps = 16.7 ms).
    private func frameStats() -> [String: Double] {
        guard let times = renderer?.frameTimes, !times.isEmpty else { return [:] }
        let s = times.sorted()
        return ["mean": (times.reduce(0, +) / Double(times.count) * 10).rounded() / 10,
                "p95": (s[Int(Double(s.count - 1) * 0.95)] * 10).rounded() / 10, "n": Double(times.count)]
    }

    nonisolated static func moved(from a: simd_float4x4, to b: simd_float4x4,
                                  move: Float = stillMove, turn: Float = stillTurn) -> Bool {
        let d = simd_distance(SIMD3(a.columns.3.x, a.columns.3.y, a.columns.3.z), SIMD3(b.columns.3.x, b.columns.3.y, b.columns.3.z))
        let fa = -SIMD3(a.columns.2.x, a.columns.2.y, a.columns.2.z), fb = -SIMD3(b.columns.2.x, b.columns.2.y, b.columns.2.z)
        return d >= move || acos(max(-1, min(1, simd_dot(simd_normalize(fa), simd_normalize(fb))))) >= turn
    }

    private func fire(auto: Bool, lowLight: Bool? = nil, close: Bool = false, gate: (Double, Double?)? = nil) async {
        guard !capturing, stage == .scanning else { return }
        capturing = true
        defer { capturing = false }
        let t0 = Date()
        // Depth for the still: the newest regular frame right now (high-res frames carry none).
        let nearest = session.currentFrame.flatMap(DepthCopy.from)
        do {
            var requested: String?
            let frame = try await HighResCapture.capture(session: session, format: videoFormat,
                                                         maxDimensions: probe?.useMaxDimensions ?? false, requested: &requested)
            let gap = nearest.map { Int(abs(frame.timestamp - $0.timestamp) * 1000) }
            // Relative sharpness gate on the still itself (motion blur shows at full resolution).
            let image = Unchecked(value: frame.capturedImage)
            let sharp = await Task.detached { Self.centerSharpness(image.value) }.value
            let median = close ? closeSharpness.median : orbitSharpness.median
            let gateLog: [String: Any] = ["still_sharpness": (sharp * 10).rounded() / 10, "still_median": median ?? -1,
                                          "preview_sharpness": gate.map { ($0.0 * 10).rounded() / 10 } ?? -1,
                                          "preview_median": gate?.1 ?? -1, "close": close,
                                          "exposure_s": frame.camera.exposureDuration, "iso_set": exposure.iso]
            if close { closeSharpness.add(sharp) } else { orbitSharpness.add(sharp) }
            if auto, let median, sharp < median * Self.stillSharpnessRatio {
                stillsRejected += 1
                diag.log("still_rejected", gateLog.merging(["reason": "blurry", "ratio": (sharp / median * 100).rounded() / 100,
                                                            "rejected_total": stillsRejected]) { a, _ in a })
                return
            }
            let id = "\(record.scanID)_s\(stillOffset + stills.count + 1)"
            if close { closeStills += 1 }
            let pos = SIMD3(frame.camera.transform.columns.3.x, frame.camera.transform.columns.3.y, frame.camera.transform.columns.3.z)
            let cell = box.flatMap { CoverageDome.cell(camera: pos, box: $0) }
            // ISO: the frame's EXIF when ARKit fills it, else the ISO the fast-shutter exposure set.
            let exif = frame.exifData
            let iso = ((exif["{Exif}"] as? [String: Any])?["ISOSpeedRatings"] as? [NSNumber])?.first?.doubleValue
                ?? (exif["ISOSpeedRatings"] as? [NSNumber])?.first?.doubleValue
                ?? (exposure.active ? Double(exposure.iso) : nil)
            let still = try await StillBuilder.write(.init(
                frame: frame, stillID: id, scanID: record.scanID, sessionID: record.id,
                targetID: close ? "closeup_\(closeStills)" : nil, targetReason: nil,
                viewCell: cell.map(CoverageDome.id), lowLight: lowLight ?? lastResult?.lowLight ?? false,
                torchLevel: torch.applied, autoFired: auto, segment: segment, sameFrame: aligned, jpegQuality: close ? 0.8 : 0.65,
                nearestDepth: frame.sceneDepth == nil ? nearest : nil, depthGapMs: frame.sceneDepth == nil ? gap : 0,
                iso: iso, stillsDir: storage.stillsDir(record)))
            stills.append(id)
            shotTick += 1
            diag.log("still", ["id": id, "segment": segment, "auto": auto, "w": still.widthPx, "h": still.heightPx,
                               "ms": Int(Date().timeIntervalSince(t0) * 1000), "depth": still.depthFile != nil,
                               "depth_gap_ms": gap ?? -1, "iso": iso ?? -1, "exposure_s": still.exposureS ?? -1,
                               "low_light": still.lowLight ?? false, "torch": torch.applied, "cell": still.viewCell ?? "",
                               "since_scan_s": scanTapped.map { (Date().timeIntervalSince($0) * 10).rounded() / 10 } ?? -1,
                               "kind": close ? "closeup" : "orbit", "center_depth_m": centerDepth ?? -1,
                               "focus_position": ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera?.lensPosition ?? -1,
                               "focus_mode": ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera?.focusMode.rawValue ?? -1,
                               "focusing": ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera?.isAdjustingFocus ?? false,
                               "view_distance_m": box.flatMap { Self.rayBoxDistance(cameraToWorld: frame.camera.transform, box: $0) } ?? -1,
                               "bytes": (try? FileManager.default.attributesOfItem(atPath: storage.stillsDir(record).appending(path: "\(id).jpg").path)[.size] as? Int) ?? -1]
                .merging(gateLog) { a, _ in a })
            upload?.enqueueStill(id)
        } catch {
            diag.log("still_failed", ["error": error.localizedDescription])
        }
    }
}

extension ScanSession: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        if Thread.isMainThread {
            MainActor.assumeIsolated { self.handle(frame) }
        }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        let message = error.localizedDescription
        Task { @MainActor in
            self.diag.log("error", ["where": "ar_session", "message": message])
            self.stage = .failed(message)
        }
    }

    nonisolated func sessionWasInterrupted(_ session: ARSession) {
        Task { @MainActor in self.diag.log("interrupted") }
    }

    nonisolated func sessionInterruptionEnded(_ session: ARSession) {
        Task { @MainActor in self.diag.log("interruption_ended") }
    }

    nonisolated func sessionShouldAttemptRelocalization(_ session: ARSession) -> Bool { true }
}

/// The active capture (deletes refuse the session being captured).
@MainActor
enum CaptureRegistry {
    static var active: String?
}

/// ~5 Hz recording of depth + confidence + 640 px RGB + pose + intrinsics as scan keyframes, written on a
/// background queue (at most 2 frames in flight; a busy writer skips a frame rather than holding ARKit buffers).
@MainActor
final class FrameRecorder {
    nonisolated static let rgbWidth = 640
    private let scanDir: URL
    private let prefix: String
    private let queue = DispatchQueue(label: "frame-recorder", qos: .utility)
    private var inFlight = 0
    private var next = 0
    private var start: TimeInterval?
    private(set) var keyframes: [ScanPackageSchema] = []
    var onWritten: ((ScanPackageSchema) -> Void)?

    init(scanDir: URL, prefix: String) {
        self.scanDir = scanDir
        self.prefix = prefix
    }

    /// A continued scan numbers its keyframes after the earlier ones.
    func startIndex(_ n: Int) { next = n }

    var count: Int { keyframes.count }

    func record(_ frame: ARFrame, segment: Int) {
        guard inFlight < 2, let depth = DepthCopy.from(frame) else { return }
        if start == nil { start = frame.timestamp }
        let id = String(format: "kf_%04d", next)
        next += 1
        inFlight += 1
        let image = Unchecked(value: frame.capturedImage)
        let iw = Float(CVPixelBufferGetWidth(frame.capturedImage)), ih = Float(CVPixelBufferGetHeight(frame.capturedImage))
        let k = frame.camera.intrinsics, m = frame.camera.transform, q = simd_quatf(m)
        let stamp = frame.timestamp - (start ?? frame.timestamp)
        let exposure = frame.camera.exposureDuration
        let tracking: TrackingState = { if case .normal = frame.camera.trackingState { return .normal } else { return .limited } }()
        let dir = scanDir, prefix = prefix
        queue.async {
            let result = Result { () throws -> ScanPackageSchema in
                let scale = Double(Self.rgbWidth) / Double(iw)
                // Small RGB for TSDF colouring: q 0.6 keeps a 640 px frame near 25 KB (depth PNGs stay lossless).
                let (w, h) = try ImageWriters.writeJPEG(image.value, to: dir.appending(path: "\(id).jpg"), quality: 0.6, scale: scale)
                try ImageWriters.writeDepthPNG(depth.depth, width: depth.width, height: depth.height, to: dir.appending(path: "\(id)_depth.png"))
                try depth.confidence.withUnsafeBytes {
                    try ImageWriters.writePNG(bytes: $0, width: depth.width, height: depth.height, bitsPerComponent: 8,
                                              bytesPerRow: depth.width, bitmapInfo: CGBitmapInfo(), to: dir.appending(path: "\(id)_conf.png"))
                }
                let sx = Double(w) / Double(iw), sy = Double(h) / Double(ih)
                return ScanPackageSchema(
                    camera: KeyframeCamera(frame: .arkitWorld, positionM: [Double(m.columns.3.x), Double(m.columns.3.y), Double(m.columns.3.z)],
                                           quaternionXyzw: [Double(q.imag.x), Double(q.imag.y), Double(q.imag.z), Double(q.real)]),
                    confidenceFile: "\(prefix)/\(id)_conf.png", depthEncoding: .uint16Mm, depthFile: "\(prefix)/\(id)_depth.png",
                    exposureS: exposure > 0 ? exposure : nil, imageFile: "\(prefix)/\(id).jpg",
                    intrinsics: KeyframeIntrinsics(cx: Double(k[2][0]) * sx, cy: Double(k[2][1]) * sy, fx: Double(k[0][0]) * sx,
                                                   fy: Double(k[1][1]) * sy, heightPx: h, widthPx: w),
                    keyframeID: id, segment: segment == 0 ? nil : segment, sharpness: nil, timestampS: stamp, trackingState: tracking)
            }
            Task { @MainActor in
                self.inFlight -= 1
                if case .success(let kf) = result {
                    self.keyframes.append(kf)
                    self.onWritten?(kf)
                }
            }
        }
    }

    func drain() async -> [ScanPackageSchema] {
        while inFlight > 0 { try? await Task.sleep(for: .milliseconds(20)) }
        return keyframes.sorted { $0.timestampS < $1.timestampS }
    }
}

/// The ~10 Hz quality pass, off the main thread, on a ≤ 256 px luma copy.
actor FrameAnalyzer {
    struct Input: Sendable {
        var luma: [UInt8]
        var width: Int, height: Int
        /// The object's projected bounds in luma pixels (for the glare measure), when locked.
        var objectRect: (x0: Int, y0: Int, x1: Int, y1: Int)?
        var ambient: Double?
        var centerDepth: Float?
        var projection: Projection

        @MainActor
        static func make(frame: ARFrame, box: ObjectBox?, depth: DepthCopy?) -> Input? {
            let img = frame.capturedImage
            CVPixelBufferLockBaseAddress(img, .readOnly)
            defer { CVPixelBufferUnlockBaseAddress(img, .readOnly) }
            guard let base = CVPixelBufferGetBaseAddressOfPlane(img, 0) else { return nil }
            let iw = CVPixelBufferGetWidthOfPlane(img, 0), ih = CVPixelBufferGetHeightOfPlane(img, 0)
            let row = CVPixelBufferGetBytesPerRowOfPlane(img, 0)
            let step = max(1, (iw + 255) / 256)
            let w = iw / step, h = ih / step
            let p = base.assumingMemoryBound(to: UInt8.self)
            var luma = [UInt8](repeating: 0, count: w * h)
            for y in 0..<h { for x in 0..<w { luma[y * w + x] = p[y * step * row + x * step] } }
            let k = frame.camera.intrinsics
            let proj = Projection(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1], width: Float(iw), height: Float(ih))
            var rect: (Int, Int, Int, Int)?
            if let box {
                let pts = box.corners.compactMap { proj.project($0, cameraToWorld: frame.camera.transform) }
                if pts.count == 8 {
                    let s = Float(step)
                    rect = (max(0, Int(pts.map(\.x).min()! / s)), max(0, Int(pts.map(\.y).min()! / s)),
                            min(w, Int(pts.map(\.x).max()! / s)), min(h, Int(pts.map(\.y).max()! / s)))
                }
            }
            return Input(luma: luma, width: w, height: h, objectRect: rect,
                         ambient: frame.lightEstimate.map { Double($0.ambientIntensity) }, centerDepth: nil, projection: proj)
        }
    }

    struct Output: Sendable {
        var sharpness: Double
        var medianSharpness: Double?
        var meanLuma: Double
        var saturated: Double
        var ambient: Double?
        /// Fine-detail energy in the centre fifth of the view, its session median, and whether the centre lies on
        /// the locked object.
        var centerDetail: Double = 0
        var centerDetailMedian: Double?
        var centerInObject = false
    }

    private var median = RunningMedian()
    private var detailMedian = RunningMedian(capacity: 80)

    func analyze(_ i: Input) -> Output {
        let sharp = QualityGate.laplacianVariance(i.luma, width: i.width, height: i.height)
        let med = median.median
        median.add(sharp)
        let mean = Double(i.luma.reduce(0) { $0 + Int($1) }) / Double(max(1, i.luma.count)) / 255
        var sat = 0.0
        if let r = i.objectRect, r.x1 > r.x0, r.y1 > r.y0 {
            var n = 0, hot = 0
            for y in r.y0..<r.y1 { for x in r.x0..<r.x1 {
                n += 1
                if i.luma[y * i.width + x] > 247 { hot += 1 }
            } }
            sat = n > 0 ? Double(hot) / Double(n) : 0
        }
        // Centre patch (the middle ~20 % of each side): its own Laplacian variance.
        let cw = max(8, i.width / 5), ch = max(8, i.height / 5), x0 = (i.width - cw) / 2, y0 = (i.height - ch) / 2
        var patch = [UInt8](repeating: 0, count: cw * ch)
        for y in 0..<ch { for x in 0..<cw { patch[y * cw + x] = i.luma[(y0 + y) * i.width + x0 + x] } }
        let detail = QualityGate.laplacianVariance(patch, width: cw, height: ch)
        var inObject = false
        if let r = i.objectRect { inObject = i.width / 2 >= r.x0 && i.width / 2 < r.x1 && i.height / 2 >= r.y0 && i.height / 2 < r.y1 }
        let detailMed = detailMedian.median
        if inObject { detailMedian.add(detail) }
        return Output(sharpness: sharp, medianSharpness: med, meanLuma: mean, saturated: sat, ambient: i.ambient,
                      centerDetail: detail, centerDetailMedian: detailMed, centerInObject: inObject)
    }
}

/// The generated contract type for a later capture segment.
typealias Segment_ = RepairOS.Segment
