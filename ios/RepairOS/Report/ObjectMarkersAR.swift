import ARKit
import Combine
import RealityKit
import SwiftUI
import UIKit

// "Show on object": the report's located findings as numbered markers on the real object. The AR session
// relocalises into the scan's saved ARWorldMap (the mechanism "Continue scanning" uses), so `location.point_m`
// (the scan's ARKit world frame) lands on the object again. Markers are small unlit billboards, depth-tested
// against the LiDAR scene mesh (RealityKit occlusion); the top problem pulses gently. Tapping a marker opens that
// finding's card. Relocalising longer than `ObjectMarkersModel.timeout` says so and offers the 3D view.

/// One located finding to place.
struct ObjectMarker: Identifiable, Sendable, Equatable {
    var id: String
    /// Rank among the most likely problems (1…3); nil draws a small unnumbered dot.
    var number: Int?
    var point: SIMD3<Float>
    var tone: Tone
}

/// What "Show on object" needs from a scan on the phone.
struct ObjectSpace: Sendable {
    var sessionDir: URL
    /// The locked box centre (scan package `crop_box`), to push markers off the surface, outward.
    var center: SIMD3<Float>?
    /// The scan's first keyframe (sensor orientation): where the scan started, shown as a "point from here" hint.
    var hintImage: URL?
    /// The saved in-box cloud and the support plane height, for the object-level alignment.
    var cloudURL: URL?
    var planeY: Float?

    var worldMapURL: URL { sessionDir.appending(path: "world.arworldmap") }

    /// Present only for a scan with a saved world map on a phone that can track the world.
    static func load(sessionDir: URL, scanPackage: URL) -> ObjectSpace? {
        guard ARWorldTrackingConfiguration.isSupported,
              FileManager.default.fileExists(atPath: sessionDir.appending(path: "world.arworldmap").path) else { return nil }
        var center: SIMD3<Float>?
        var planeY: Float?
        if let data = try? Data(contentsOf: scanPackage),
           let pkg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let box = pkg["crop_box"] as? [String: Any],
           let c = box["center_m"] as? [NSNumber], c.count == 3 {
            center = SIMD3(c[0].floatValue, c[1].floatValue, c[2].floatValue)
            if let e = box["extent_m"] as? [NSNumber], e.count == 3 { planeY = c[1].floatValue - e[1].floatValue / 2 }
        }
        let scanDir = scanPackage.deletingLastPathComponent()
        let first = scanDir.appending(path: "kf_0000.jpg")
        return ObjectSpace(sessionDir: sessionDir, center: center,
                           hintImage: FileManager.default.fileExists(atPath: first.path) ? first : nil,
                           cloudURL: ModelSource.cloudFallback(in: scanDir), planeY: planeY)
    }
}

@MainActor
@Observable
final class ObjectMarkersModel {
    enum Phase: Equatable { case loading, relocalizing, found, timedOut, failed(String) }

    static let timeout: TimeInterval = 6

    private(set) var phase: Phase = .loading
    /// Seconds from the session start to the match (nil until it matched).
    private(set) var relocalizedAfter: Double?
    var selected: String?
    private(set) var startedAt = Date()
    private var timer: Task<Void, Never>?

    func started() {
        startedAt = Date()
        phase = .relocalizing
        armTimer()
    }

    func retry() {
        guard phase == .timedOut else { return }
        phase = .relocalizing
        armTimer()
    }

    private func armTimer() {
        timer?.cancel()
        timer = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.timeout))
            guard !Task.isCancelled, let self, self.phase == .relocalizing else { return }
            self.phase = .timedOut
        }
    }

    func matched() {
        guard phase == .relocalizing || phase == .timedOut else { return }
        timer?.cancel()
        relocalizedAfter = Date().timeIntervalSince(startedAt)
        phase = .found
    }

    func fail(_ message: String) {
        timer?.cancel()
        phase = .failed(message)
    }
}

/// Full-screen AR: the camera with the markers, a cue while relocalising, the numbered problems along the bottom.
struct ObjectMarkersView: View {
    let space: ObjectSpace
    let markers: [ObjectMarker]
    /// Every problem on the report (the card for a tapped marker).
    let problems: [ReportModel.Problem]
    /// Findings beyond the top three: listed behind "+N", not drawn.
    var others: [ReportModel.Problem] = []
    let show3D: () -> Void
    let close: () -> Void

    @State private var model = ObjectMarkersModel()
    @State private var card: ReportModel.Problem?
    @State private var showUnplaced = false
    @State private var foundTick = 0
    @State private var hint: UIImage?
    @Environment(\.cropSource) private var cropSource

    /// Top problems without a position, then the rest.
    private var unplaced: [ReportModel.Problem] {
        let placed = Set(markers.map(\.id)), rest = Set(others.map(\.id))
        return problems.filter { !placed.contains($0.id) && !rest.contains($0.id) && $0.issue != ReportModel.noIssue } + others
    }

    var body: some View {
        ZStack {
            ObjectMarkersARView(space: space, markers: markers, model: model) { id in
                card = problems.first { $0.id == id }
            }
            .ignoresSafeArea()

            if model.phase == .relocalizing || model.phase == .loading {
                LockBrackets(phase: .searching)
                    .frame(width: 180, height: 180)
                    .opacity(0.8)
                    .transition(.opacity)
            }

            VStack(spacing: Spacing.m) {
                HStack(alignment: .top) {
                    Button(action: close) { Image(systemName: Symbol.close) }
                        .buttonStyle(RoundIconButtonStyle(onCamera: true))
                        .accessibilityLabel("Close")
                    Spacer()
                    Button(action: show3D) { Image(systemName: "rotate.3d") }
                        .buttonStyle(RoundIconButtonStyle(onCamera: true))
                        .accessibilityLabel("Show the 3D view")
                }
                Spacer()
                if model.phase == .relocalizing || model.phase == .timedOut, let hint {
                    VStack(spacing: 6) {
                        Image(uiImage: hint).resizable().scaledToFill()
                            .frame(width: 120, height: 90)
                            .rotationEffect(.degrees(90))
                            .frame(width: 90, height: 120)
                            .clipShape(RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: Radius.medium, style: .continuous).strokeBorder(.white.opacity(0.5), lineWidth: 1))
                        Text("The scan started here").font(.caption.weight(.semibold)).foregroundStyle(.white)
                    }
                    .padding(8)
                    .background(Color.black.opacity(0.5), in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                    .transition(.opacity)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("A photo of where the scan started")
                }
                cue
                if model.phase == .timedOut { timedOutCard.transition(.move(edge: .bottom).combined(with: .opacity)) }
                if model.phase == .found { problemStrip.transition(.move(edge: .bottom).combined(with: .opacity)) }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
        .motion(Motion.standard, value: model.phase)
        .sensoryFeedback(Haptic.milestone, trigger: foundTick)
        .onChange(of: model.phase) { _, new in if new == .found { foundTick += 1 } }
        .task {
            guard let url = space.hintImage, let data = try? Data(contentsOf: url) else { return }
            hint = UIImage(data: data)
        }
        .statusBarHidden()
        .sheet(item: $card) { p in
            NavigationStack {
                ProblemDetailView(problem: p)
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { card = nil }.fontWeight(.semibold) } }
            }
            .environment(\.cropSource, cropSource)
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showUnplaced) {
            NavigationStack {
                List(unplaced) { p in
                    Button { showUnplaced = false; card = p } label: {
                        HStack(spacing: Spacing.m) {
                            CropThumb(sha: p.crops.first, cornerRadius: Radius.small).frame(width: 44, height: 44)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                                Text(p.issue).font(.footnote).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                .navigationTitle("Other findings").navigationBarTitleDisplayMode(.inline)
                .toolbar { Button("Done") { showUnplaced = false }.fontWeight(.semibold) }
            }
            .environment(\.cropSource, cropSource)
            .presentationDetents([.medium])
        }
    }

    @ViewBuilder private var cue: some View {
        switch model.phase {
        case .loading, .relocalizing:
            CueBanner(text: "Point at the object from where you scanned it", symbol: Symbol.scan)
        case .found:
            CueBanner(text: markers.count == 1 ? "1 problem on the object" : "\(markers.count) problems on the object",
                      symbol: "checkmark", tone: .good)
        case .timedOut:
            CueBanner(text: "Can't line up with the object yet", symbol: "exclamationmark", tone: .caution)
        case .failed(let message):
            CueBanner(text: message, symbol: "xmark", tone: .stop)
        }
    }

    private var timedOutCard: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("The phone hasn't recognised the scene from the scan. Stand where you scanned and point at the object, or look at it in 3D instead.")
                .font(.subheadline).foregroundStyle(.white).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Spacing.s) {
                Button { model.retry() } label: { Label("Keep trying", systemImage: "arrow.clockwise") }
                    .buttonStyle(.primary(.light))
                Button(action: show3D) { Label("3D view", systemImage: "rotate.3d") }
                    .buttonStyle(.primary(.light))
            }
        }
        .padding(Spacing.l)
        .background(Color.black.opacity(0.62), in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }

    /// The numbered problems, tap for the card; plus the ones with no position.
    private var problemStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Spacing.s) {
                ForEach(markers.filter { $0.number != nil }) { m in
                    if let p = problems.first(where: { $0.id == m.id }) {
                        Button { card = p } label: {
                            HStack(spacing: 8) {
                                Text("\(m.number ?? 0)")
                                    .font(.system(.footnote, design: .rounded, weight: .bold)).monospacedDigit()
                                    .foregroundStyle(.white)
                                    .frame(width: 24, height: 24)
                                    .background(m.tone.color, in: Circle())
                                VStack(alignment: .leading, spacing: 0) {
                                    Text(p.part).font(.footnote.weight(.semibold)).lineLimit(1)
                                    Text(p.issue).font(.caption).opacity(0.75).lineLimit(1)
                                }
                                .foregroundStyle(.white)
                            }
                            .padding(.leading, 6).padding(.trailing, 14).padding(.vertical, 6)
                            .background(Color.black.opacity(0.58), in: Capsule())
                        }
                        .buttonStyle(PressableCardStyle())
                    }
                }
                if !unplaced.isEmpty {
                    Button { showUnplaced = true } label: {
                        StatusPill(text: "+\(unplaced.count) more", symbol: "list.bullet", onCamera: true)
                    }
                    .buttonStyle(PressableCardStyle())
                }
            }
        }
        .scrollClipDisabled()
    }
}

// MARK: - AR view

private struct ObjectMarkersARView: UIViewRepresentable {
    let space: ObjectSpace
    let markers: [ObjectMarker]
    let model: ObjectMarkersModel
    let onTap: (String) -> Void

    func makeCoordinator() -> MarkerScene { MarkerScene(space: space, markers: markers, model: model) }

    func makeUIView(context: Context) -> ARView {
        let view = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
        context.coordinator.attach(view)
        return view
    }

    func updateUIView(_ view: ARView, context: Context) {
        context.coordinator.onTap = onTap
    }

    static func dismantleUIView(_ view: ARView, coordinator: MarkerScene) {
        coordinator.stop()
    }
}

/// Owns the AR session and the marker entities.
@MainActor
final class MarkerScene: NSObject, ARSessionDelegate {
    private let space: ObjectSpace
    private let markers: [ObjectMarker]
    private let model: ObjectMarkersModel
    private weak var view: ARView?
    private var root: AnchorEntity?
    /// Which answer placed the markers: the saved world map (authoritative) or the object alignment.
    private enum Placed { case none, object, map }
    private var placedBy = Placed.none
    private var objectPose: simd_float4x4?
    /// The last accepted alignment, waiting for the next one to agree (two in a row within 1.5 cm) before markers show:
    /// a single fit on a flat or symmetric object can be confidently wrong.
    private var pendingPose: simd_float4x4?
    static let agreement: Float = 0.015
    // Object alignment: the saved object, the live LiDAR points (5 mm cells), attempt bookkeeping.
    private var target: AlignTarget?
    private var live: [SIMD3<Int32>: LivePoint] = [:]
    private var lastSample: TimeInterval = 0
    private var lastAttempt: TimeInterval = 0
    private var lastWaitLog: TimeInterval = 0
    private var aligning = false
    private var attempts = 0
    private var aim: SIMD3<Float>?
    private var cameraPos: SIMD3<Float> = .zero
    private var tracking = ""
    private var startedAt = Date()
    /// The camera in this session's own frame on the last frame before the world map matched (to compare answers).
    private var lastUnmatchedCamera: simd_float4x4?
    private var updates: Cancellable?
    private var halo: ModelEntity?
    private var badges: [Entity] = []
    private var dropStart: TimeInterval?
    private var clock: TimeInterval = 0
    private var frameTimes: [Double] = []
    private let diag: Diag
    var onTap: (String) -> Void = { _ in }

    /// Numbered badge side and dot side, metres at the reference distance.
    static let badgeSide: Float = 0.018
    static let dotSide: Float = 0.009
    static let referenceDistance: Float = 0.45
    /// Badges float this far off the surface (outward from the object), more when several are close together.
    static let lift: Float = 0.022
    static let stack: Float = 0.02

    init(space: ObjectSpace, markers: [ObjectMarker], model: ObjectMarkersModel) {
        self.space = space
        self.markers = markers
        self.model = model
        diag = Diag.session(space.sessionDir)
    }

    func attach(_ view: ARView) {
        self.view = view
        view.renderOptions = [.disableMotionBlur, .disableDepthOfField, .disableCameraGrain, .disableGroundingShadows,
                              .disableHDR, .disableFaceMesh, .disablePersonOcclusion, .disableAREnvironmentLighting]
        view.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tap(_:))))
        let map = ScanSession.loadWorldMap(space.sessionDir)
        guard let map else {
            Task { @MainActor in self.model.fail("This scan has no saved position") }
            return
        }
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        config.initialWorldMap = map
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            config.sceneReconstruction = .mesh
            view.environment.sceneUnderstanding.options = [.occlusion]
        }
        // For the object alignment that runs alongside: LiDAR depth and the table plane.
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { config.frameSemantics.insert(.sceneDepth) }
        config.planeDetection = [.horizontal]
        startedAt = Date()
        if let url = space.cloudURL {
            let planeY = space.planeY
            Task.detached(priority: .userInitiated) {
                let t0 = Date()
                guard let pts = ScanCloudReader.read(url) else { return }
                let target = AlignTarget.make(points: pts.map(\.0), colors: pts.map { SIMD3<Float>($0.1) / 255 }, planeY: planeY)
                await MainActor.run {
                    self.target = target
                    self.diag.log("ar_align_ready", ["points": target?.points.count ?? 0, "ms": Int(Date().timeIntervalSince(t0) * 1000)])
                }
            }
        }
        view.session.delegate = self
        view.session.run(config, options: [.resetTracking, .removeExistingAnchors])
        // Not during SwiftUI's view update (makeUIView): the next main-actor turn.
        Task { @MainActor in self.model.started() }
        diag.log("ar_markers_start", ["markers": markers.count, "map_anchors": map.anchors.count])
        updates = view.scene.subscribe(to: SceneEvents.Update.self) { [weak self] e in
            MainActor.assumeIsolated { self?.update(dt: e.deltaTime) }
        }
    }

    func stop() {
        updates?.cancel()
        view?.session.pause()
        if !frameTimes.isEmpty {
            let s = frameTimes.sorted()
            diag.log("ar_markers_end", ["frame_ms_mean": (frameTimes.reduce(0, +) / Double(frameTimes.count) * 10).rounded() / 10,
                                        "frame_ms_p95": (s[Int(Double(s.count - 1) * 0.95)] * 10).rounded() / 10,
                                        "frames": frameTimes.count, "relocalized_s": model.relocalizedAfter ?? -1])
        }
    }

    // MARK: Session

    nonisolated func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        let normal: Bool = { if case .normal = camera.trackingState { true } else { false } }()
        let state = "\(camera.trackingState)"
        Task { @MainActor in
            self.tracking = state
            guard normal, self.placedBy != .map else { return }
            // The saved world map matched: authoritative. It moves this session's world origin, so markers placed by
            // the object alignment (in the old frame) are placed again in the map's frame.
            let had = self.placedBy
            var fields: [String: Any] = ["after_ms": Int(Date().timeIntervalSince(self.startedAt) * 1000), "state": state,
                                         "object_first": had == .object]
            if had == .object {
                // How far the object alignment was from the world map's answer (both in the map's frame now).
                if let pose = self.objectPose, let before = self.lastUnmatchedCamera,
                   let after = self.view?.session.currentFrame?.camera.transform, let c = self.space.center {
                    let toMap = after * simd_inverse(before)
                    let d = ObjectAligner.poseDistance(toMap * pose, matrix_identity_float4x4, at: c, reach: self.target?.reach ?? 0.1)
                    fields["object_vs_map_mm"] = Int((d * 1000).rounded())
                }
                self.root?.removeFromParent(); self.root = nil; self.badges = []; self.halo = nil
            }
            self.placedBy = .map
            self.live = [:]
            self.placeMarkers(frame: matrix_identity_float4x4, animated: had == .none)
            self.model.matched()
            self.diag.log("ar_markers_relocalized", fields)
        }
    }

    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard Thread.isMainThread else { return }
        let normal: Bool = { if case .normal = frame.camera.trackingState { true } else { false } }()
        let m = frame.camera.transform
        MainActor.assumeIsolated {
            if !normal || self.placedBy == .none { self.lastUnmatchedCamera = m }
            self.sample(frame)
        }
    }

    // MARK: Object alignment

    /// ~5 Hz: the LiDAR points around the view into 5 mm cells (this session's frame), the aim and the camera; then an
    /// alignment attempt off the main thread when the last one is done.
    private func sample(_ frame: ARFrame) {
        guard placedBy == .none, target != nil else { return }
        let t = frame.timestamp
        guard t - lastSample >= 0.2 else { return }
        lastSample = t
        let m = frame.camera.transform
        cameraPos = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        if let pts = Self.livePoints(frame) {
            for q in pts.points { live[VoxelGrid.key(q.p, 0.005)] = q }
            if let a = pts.aim { aim = a }
            if live.count > 40_000 { live = live.filter { _ in Bool.random() } }
        }
        if t - lastWaitLog >= 3, placedBy == .none {
            lastWaitLog = t
            diag.log("ar_align_wait", ["live_points": live.count, "aim": aim != nil, "tracking": tracking, "attempts": attempts,
                                       "camera": [cameraPos.x, cameraPos.y, cameraPos.z].map { ($0 * 1000).rounded() / 1000 },
                                       "since_start_ms": Int(Date().timeIntervalSince(startedAt) * 1000)])
        }
        guard !aligning, t - lastAttempt >= 0.45, let target, let aim, live.count >= 400 else { return }
        lastAttempt = t
        aligning = true
        attempts += 1
        let points = Array(live.values), camera = cameraPos
        let plane = Self.planeBelow(aim, anchors: frame.anchors)
        let attempt = attempts, state = tracking
        Task.detached(priority: .userInitiated) {
            let t0 = Date()
            let r = ObjectAligner.align(target: target, live: points, aim: aim, planeY: plane, camera: camera)
            let ms = Int(Date().timeIntervalSince(t0) * 1000)
            await MainActor.run { self.aligned(r, attempt: attempt, ms: ms, livePoints: points.count, state: state, camera: camera) }
        }
    }

    private func aligned(_ r: ObjectAligner.Result?, attempt: Int, ms: Int, livePoints: Int, state: String, camera: SIMD3<Float>) {
        aligning = false
        var fields: [String: Any] = ["attempt": attempt, "align_ms": ms, "live_points": livePoints, "tracking": state,
                                     "since_start_ms": Int(Date().timeIntervalSince(startedAt) * 1000)]
        if let r {
            fields.merge(["accepted": r.accepted, "refused": r.refused ?? "", "rms_mm": (r.rms * 10000).rounded() / 10,
                          "inliers": (r.inliers * 100).rounded() / 100, "coverage": (r.coverage * 100).rounded() / 100,
                          "explained": (r.explained * 100).rounded() / 100, "object_points": r.livePoints]) { a, _ in a }
        } else {
            fields["accepted"] = false
            fields["refused"] = "no object"
        }
        guard let r, r.accepted, placedBy == .none else {
            if attempt <= 3 || attempt % 4 == 0 { diag.log("ar_align_attempt", fields) }
            return
        }
        let reach = target?.reach ?? 0.1, c = space.center ?? .zero
        let gap = pendingPose.map { ObjectAligner.poseDistance($0, r.savedToLive, at: c, reach: reach) }
        guard let gap, gap < Self.agreement else {
            if let gap { fields["disagreed_mm"] = Int(gap * 1000) }
            pendingPose = r.savedToLive
            diag.log("ar_align_attempt", fields.merging(["pending": true]) { a, _ in a })
            return
        }
        // Which side of the object the phone is on (saved frame), for "from any side" checks.
        let camSaved = (simd_inverse(r.savedToLive) * SIMD4(camera, 1)).xyz
        if let c = space.center {
            fields["side_deg"] = Int((CoverageDome.azimuth(camSaved - c) * 180 / .pi).rounded())
        }
        placedBy = .object
        objectPose = r.savedToLive
        placeMarkers(frame: r.savedToLive)
        model.matched()
        diag.log("ar_markers_object_aligned", fields)
    }

    /// The table under the aim: ARKit's horizontal plane nearest below it.
    private static func planeBelow(_ aim: SIMD3<Float>, anchors: [ARAnchor]) -> Float? {
        anchors.compactMap { $0 as? ARPlaneAnchor }.filter { $0.alignment == .horizontal }
            .map { $0.transform.columns.3.y }
            .filter { $0 < aim.y + 0.01 && aim.y - $0 < 0.6 }
            .max()
    }

    /// LiDAR points (confidence ≥ medium, nearer than 1.5 m) from the middle 80 % of the view, every 2nd depth pixel,
    /// coloured from the camera image; and where the view centre meets the scene.
    static func livePoints(_ frame: ARFrame) -> (points: [LivePoint], aim: SIMD3<Float>?)? {
        guard let depth = frame.sceneDepth, let conf = depth.confidenceMap else { return nil }
        let dm = depth.depthMap, img = frame.capturedImage
        CVPixelBufferLockBaseAddress(dm, .readOnly); CVPixelBufferLockBaseAddress(conf, .readOnly)
        CVPixelBufferLockBaseAddress(img, .readOnly)
        defer {
            CVPixelBufferUnlockBaseAddress(dm, .readOnly); CVPixelBufferUnlockBaseAddress(conf, .readOnly)
            CVPixelBufferUnlockBaseAddress(img, .readOnly)
        }
        guard let dBase = CVPixelBufferGetBaseAddress(dm), let cBase = CVPixelBufferGetBaseAddress(conf),
              let yBase = CVPixelBufferGetBaseAddressOfPlane(img, 0), let uvBase = CVPixelBufferGetBaseAddressOfPlane(img, 1) else { return nil }
        let w = CVPixelBufferGetWidth(dm), h = CVPixelBufferGetHeight(dm)
        let dRow = CVPixelBufferGetBytesPerRow(dm), cRow = CVPixelBufferGetBytesPerRow(conf)
        let iw = CVPixelBufferGetWidthOfPlane(img, 0), ih = CVPixelBufferGetHeightOfPlane(img, 0)
        let yRow = CVPixelBufferGetBytesPerRowOfPlane(img, 0), uvRow = CVPixelBufferGetBytesPerRowOfPlane(img, 1)
        var k = frame.camera.intrinsics
        let sx = Float(w) / Float(iw), sy = Float(h) / Float(ih)
        k[0][0] *= sx; k[2][0] *= sx; k[1][1] *= sy; k[2][1] *= sy
        let m = frame.camera.transform
        var out: [LivePoint] = []
        out.reserveCapacity(w * h / 4)
        var aim: SIMD3<Float>?
        var aimD2 = 101
        for v in stride(from: h / 10, to: h - h / 10, by: 2) {
            let dr = (dBase + v * dRow).assumingMemoryBound(to: Float32.self)
            let cr = (cBase + v * cRow).assumingMemoryBound(to: UInt8.self)
            for u in stride(from: w / 10, to: w - w / 10, by: 2) {
                let d = dr[u]
                guard cr[u] >= 1, d > 0.1, d < 1.5 else { continue }
                let pc = SIMD3((Float(u) - k[2][0]) * d / k[0][0], -(Float(v) - k[2][1]) * d / k[1][1], -d)
                let wp = (m * SIMD4(pc, 1)).xyz
                let ix = min(iw - 1, Int(Float(u) / sx)), iy = min(ih - 1, Int(Float(v) / sy))
                let y = Float((yBase + iy * yRow).assumingMemoryBound(to: UInt8.self)[ix]) / 255
                let uv = (uvBase + (iy / 2) * uvRow).assumingMemoryBound(to: UInt8.self)
                let cb = Float(uv[(ix / 2) * 2]) / 255 - 0.5, cr2 = Float(uv[(ix / 2) * 2 + 1]) / 255 - 0.5
                let rgb = simd_clamp(SIMD3(y + 1.402 * cr2, y - 0.3441 * cb - 0.7141 * cr2, y + 1.772 * cb), .zero, SIMD3(repeating: 1))
                out.append(LivePoint(p: wp, rgb: rgb))
                // The aim: the valid pixel nearest the view centre (within 10 depth pixels).
                let d2 = (v - h / 2) * (v - h / 2) + (u - w / 2) * (u - w / 2)
                if d2 < aimD2 { aimD2 = d2; aim = wp }
            }
        }
        return (out, aim)
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        let message = error.localizedDescription
        Task { @MainActor in
            self.diag.log("error", ["where": "ar_markers", "message": message])
            self.model.fail("The camera stopped: \(message)")
        }
    }

    nonisolated func sessionShouldAttemptRelocalization(_ session: ARSession) -> Bool { true }

    // MARK: Markers

    /// `frame`: saved-scan frame → this session's world (identity once the world map matched).
    private func placeMarkers(frame: simd_float4x4, animated: Bool = true) {
        guard let view else { return }
        let root = AnchorEntity(world: frame)
        // Numbered first so the stacking puts the top problems nearest the surface.
        let ordered = markers.sorted { ($0.number ?? 99) < ($1.number ?? 99) }
        var placed: [SIMD3<Float>] = []
        for (i, m) in ordered.enumerated() {
            let crowd = placed.filter { simd_distance($0, m.point) < 0.025 }.count
            placed.append(m.point)
            let out = outward(m.point)
            let badgePos = m.point + out * (Self.lift + Float(crowd) * Self.stack)
            let side = m.number == nil ? Self.dotSide : Self.badgeSide

            let group = Entity()
            group.name = "group:\(m.id)"
            // The exact spot: a tiny dot on the surface, joined to the badge by a hairline.
            let dot = ModelEntity(mesh: .generateSphere(radius: m.number == nil ? 0.0018 : 0.0026),
                                  materials: [UnlitMaterial(color: UIColor(m.tone.color))])
            dot.position = m.point
            group.addChild(dot)
            let length = simd_distance(badgePos, m.point)
            let stem = ModelEntity(mesh: .generateBox(size: [0.0007, length, 0.0007]),
                                   materials: [Self.unlit(UIColor(white: 1, alpha: 0.85))])
            stem.position = (m.point + badgePos) / 2
            stem.orientation = simd_quatf(from: SIMD3(0, 1, 0), to: simd_normalize(badgePos - m.point))
            group.addChild(stem)

            let badge = ModelEntity(mesh: .generatePlane(width: side, height: side), materials: [Self.badgeMaterial(m)])
            badge.name = "marker:\(m.id)"
            badge.position = badgePos
            badge.components.set(BillboardComponent())
            badge.components.set(CollisionComponent(shapes: [.generateSphere(radius: side * 0.8)]))
            badge.components.set(InputTargetComponent())
            group.addChild(badge)
            badges.append(badge)

            if m.number == 1 {
                let ring = ModelEntity(mesh: .generatePlane(width: side, height: side), materials: [Self.haloMaterial(m.tone)])
                ring.position = badgePos
                ring.components.set(BillboardComponent())
                ring.components.set(OpacityComponent(opacity: 0))
                group.addChild(ring)
                halo = ring
            }
            // Drop in: start invisible and small, staggered (see `update`).
            if animated {
                group.scale = SIMD3(repeating: 0.001)
                group.components.set(OpacityComponent(opacity: 0))
                group.components.set(DropIn(delay: Double(i) * 0.07, badge: badgePos))
            }
            root.addChild(group)
        }
        view.scene.addAnchor(root)
        self.root = root
        dropStart = clock
    }

    /// Away from the object's centre, mostly sideways, a little up; straight up without a centre.
    private func outward(_ p: SIMD3<Float>) -> SIMD3<Float> {
        guard let c = space.center else { return SIMD3(0, 1, 0) }
        var d = p - c
        d.y = max(d.y, 0) + 0.35 * simd_length(SIMD2(d.x, d.z))
        return simd_length(d) > 1e-4 ? simd_normalize(d) : SIMD3(0, 1, 0)
    }

    private struct DropIn: Component { var delay: Double; var badge: SIMD3<Float> }

    private func update(dt: TimeInterval) {
        clock += dt
        if dt > 0 {
            frameTimes.append(dt * 1000)
            if frameTimes.count > 1200 { frameTimes.removeFirst(frameTimes.count - 1200) }
        }
        guard let root, let view else { return }
        let cam = view.cameraTransform.translation
        let reduceMotion = UIAccessibility.isReduceMotionEnabled
        for group in root.children {
            // Drop-in: scale about the badge (springy ease-out), fade in.
            if let drop = group.components[DropIn.self], let start = dropStart {
                let t = Float(max(0, min(1, (clock - start - drop.delay) / 0.42)))
                let s = reduceMotion ? 1 : Self.springOut(t)
                group.scale = SIMD3(repeating: max(0.001, s))
                group.position = drop.badge * (1 - max(0.001, s))
                group.components.set(OpacityComponent(opacity: t))
                if t >= 1 {
                    group.scale = .one
                    group.position = .zero
                    group.components.remove(DropIn.self)
                    group.components.remove(OpacityComponent.self)
                }
            }
        }
        // Constant-ish apparent size: grow with distance (between 0.8× and 2.2× the reference size).
        for badge in badges {
            let d = simd_distance(cam, badge.position(relativeTo: nil))
            let k = max(0.8, min(2.2, d / Self.referenceDistance))
            badge.scale = SIMD3(repeating: k)
        }
        if let halo {
            let d = simd_distance(cam, halo.position(relativeTo: nil))
            let k = max(0.8, min(2.2, d / Self.referenceDistance))
            let phase = Float((clock.truncatingRemainder(dividingBy: 1.8)) / 1.8)
            halo.scale = SIMD3(repeating: k * (reduceMotion ? 1.3 : 1 + 0.9 * phase))
            halo.components.set(OpacityComponent(opacity: reduceMotion ? 0.35 : 0.55 * (1 - phase)))
        }
    }

    /// Ease-out with a small overshoot (≈ 6 %), for the markers landing.
    private static func springOut(_ t: Float) -> Float {
        let c1: Float = 1.2, c3 = c1 + 1
        let x = t - 1
        return 1 + c3 * x * x * x + c1 * x * x
    }

    @objc private func tap(_ g: UITapGestureRecognizer) {
        guard let view else { return }
        let p = g.location(in: view)
        if let hit = view.entities(at: p).first(where: { $0.name.hasPrefix("marker:") }) {
            onTap(String(hit.name.dropFirst("marker:".count)))
            return
        }
        // Forgiving taps: the nearest badge within 36 pt of the finger.
        var best: (String, CGFloat)?
        for badge in badges {
            guard let s = view.project(badge.position(relativeTo: nil)) else { continue }
            let d = hypot(s.x - p.x, s.y - p.y)
            if d < 36, d < (best?.1 ?? .infinity) { best = (String(badge.name.dropFirst("marker:".count)), d) }
        }
        if let best { onTap(best.0) }
    }

    // MARK: Materials

    private static func unlit(_ color: UIColor) -> UnlitMaterial {
        var m = UnlitMaterial(color: color)
        if color.cgColor.alpha < 1 { m.blending = .transparent(opacity: .init(floatLiteral: Float(color.cgColor.alpha))) }
        return m
    }

    private static func textured(_ image: UIImage) -> UnlitMaterial {
        var m = UnlitMaterial()
        if let cg = image.cgImage, let tex = try? TextureResource(image: cg, options: .init(semantic: .color)) {
            m.color = .init(tint: .white, texture: .init(tex))
        }
        m.blending = .transparent(opacity: .init(floatLiteral: 1))
        return m
    }

    private static func badgeMaterial(_ m: ObjectMarker) -> UnlitMaterial {
        let side: CGFloat = m.number == nil ? 64 : 160
        let colour = UIColor(m.tone.color).resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark))
        let image = UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: .init(for: .init(displayScale: 1))).image { ctx in
            let rect = CGRect(x: 0, y: 0, width: side, height: side)
            UIColor.white.setFill()
            ctx.cgContext.fillEllipse(in: rect.insetBy(dx: side * 0.02, dy: side * 0.02))
            colour.setFill()
            ctx.cgContext.fillEllipse(in: rect.insetBy(dx: side * 0.11, dy: side * 0.11))
            guard let number = m.number else { return }
            let text = "\(number)" as NSString
            let base = UIFont.systemFont(ofSize: side * 0.5, weight: .heavy)
            let font = base.fontDescriptor.withDesign(.rounded).map { UIFont(descriptor: $0, size: base.pointSize) } ?? base
            let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
            let size = text.size(withAttributes: attrs)
            text.draw(at: CGPoint(x: (side - size.width) / 2, y: (side - size.height) / 2), withAttributes: attrs)
        }
        return textured(image)
    }

    private static func haloMaterial(_ tone: Tone) -> UnlitMaterial {
        let side: CGFloat = 160
        let colour = UIColor(tone.color).resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark))
        let image = UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: .init(for: .init(displayScale: 1))).image { ctx in
            ctx.cgContext.setStrokeColor(colour.cgColor)
            ctx.cgContext.setLineWidth(side * 0.07)
            ctx.cgContext.strokeEllipse(in: CGRect(x: 0, y: 0, width: side, height: side).insetBy(dx: side * 0.06, dy: side * 0.06))
        }
        return textured(image)
    }
}
