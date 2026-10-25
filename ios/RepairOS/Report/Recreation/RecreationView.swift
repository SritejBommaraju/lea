import SceneKit
import SwiftUI
import UIKit

// The photo-real recreation for the report's hero: the object alone (its table is cut away on both tiers; with a
// registered flipped segment the gateway's model is closed underneath and turns over), textured from the scan's own photos, framed to fill the view, soft light, a slow turntable until touched, the
// numbered problems on the surface. Transparent background: it sits on the report's dark stage.
//
// Drop-in for the report layout (the redesign owns where it goes):
//
//     if RecreationCenter.shared.available(scanDir) {
//         RecreationView(source: .scan(scanDir), markers: pins, focus: focusedID) { id in focusedID = id }
//     } else {
//         ModelView(...)                                   // the LiDAR cloud / mesh, as today
//     }
//
// and, once per report, the Tier 2 fetch (the live event and the report opening):
//
//     .task(id: live?.recreationRev) {
//         if let gateway { await RecreationCenter.shared.fetchGateway(gateway, sessionDir: dir, scanID: id, rev: live?.recreationRev) }
//     }
//
// `markers` are the hero's `ModelPin`s (the findings' `location.point_m`, the scan frame); no transform is needed.

enum RecreationSource: Equatable, Sendable {
    /// A scan folder: its best recreation (the gateway's, else the phone's shell), reloaded when a better one lands.
    case scan(URL)
    /// A recreation folder with its manifest (previews, Demo replay).
    case folder(URL)
    case none
}

struct RecreationView: View {
    let source: RecreationSource
    let markers: [ModelPin]
    var focus: String?
    var spin = true
    var onMarkerTap: (String) -> Void = { _ in }
    /// Called once the recreation is drawn (or could not be): the report stage lands its markers on it.
    var onLoaded: (() -> Void)?
    /// Circle to inspect (Report/Stage): turns a circle drawn over the view into a spot on the surface.
    var probe: StageProbe?
    /// The walkthrough playing on the stage: its pager drives the camera (TourCamera) and the turntable stops.
    var tour: WalkthroughPlayer?
    /// Lea's camera move while she answers (LeaStage): fly to a marker, look from a side, zoom.
    var cue: LeaStageCue?

    init(source: RecreationSource, markers: [ModelPin], focus: String? = nil, spin: Bool = true,
         onMarkerTap: @escaping (String) -> Void = { _ in }, onLoaded: (() -> Void)? = nil, probe: StageProbe? = nil,
         tour: WalkthroughPlayer? = nil, cue: LeaStageCue? = nil) {
        self.tour = tour
        self.cue = cue
        self.source = source
        self.markers = markers
        self.focus = focus
        self.spin = spin
        self.onLoaded = onLoaded
        self.probe = probe
        self.onMarkerTap = onMarkerTap
    }

    @State private var loaded: Loaded?
    @State private var failed = false
    @State private var visible = true
    /// Reduce Motion: the walkthrough card the hero shows (it cuts there while faded out).
    @State private var cutShown = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var center: RecreationCenter { .shared }

    struct Loaded: @unchecked Sendable {
        var id: String
        var content: RecreationContent
        var focus: (center: SIMD3<Float>, radius: Float)?
        var origin: String
        /// The underside is part of the model (RecreationManifest.closedBottom): the camera may go below it.
        var closedBottom = false
    }

    /// What this build can draw: textured meshes (PLY + atlas, or USDZ). A splat needs the splat renderer.
    nonisolated static func canDraw(_ m: RecreationManifest) -> Bool {
        // `flat_slab` (a thin object's photo slab, recreation/flat.py) is a textured mesh in a USDZ too.
        ["textured_mesh", "flat_slab"].contains(m.kind) && (m.file.hasSuffix(".ply") || m.file.hasSuffix(".usdz"))
    }

    var body: some View {
        let frame = tour.flatMap { $0.active ? $0.frame : nil }
        ZStack {
            if let loaded {
                RecreationHost(loaded: loaded, markers: markers.map(Self.marker), focus: focus, spin: spin && !reduceMotion,
                               animate: !reduceMotion, active: visible, probe: probe,
                               tour: frame?.drawn(reduceMotion: reduceMotion, shown: cutShown), cue: cue,
                               onMarkerTap: onMarkerTap)
                    .id(loaded.id)
                    .transition(.opacity)
            } else if !failed {
                ProgressView().tint(Palette.chalk)
            }
        }
        .modifier(TourCutFade(nearest: frame?.nearest, on: reduceMotion, shown: $cutShown))
        .onScrollVisibilityChange(threshold: 0.05) { visible = $0 }
        .task(id: reloadKey) { await load(); onLoaded?() }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("3D recreation of the scanned object with \(markers.count) numbered problems")
    }

    private var reloadKey: String {
        switch source {
        case .scan(let u): "\(u.path)#\(center.version[u.path] ?? 0)"
        case .folder(let u): u.path
        case .none: "none"
        }
    }

    private func load() async {
        let pick: (RecreationManifest, URL)?
        switch source {
        case .scan(let dir): pick = RecreationCenter.best(in: dir)
        case .folder(let f):
            pick = (try? Data(contentsOf: f.appending(path: "recreation.json")))
                .flatMap { try? JSONDecoder().decode(RecreationManifest.self, from: $0) }.map { ($0, f) }
        case .none: pick = nil
        }
        guard let (m, folder) = pick else { failed = loaded == nil; return }
        let id = "\(folder.path)#\(m.origin)#\(m.rev)"
        guard loaded?.id != id else { return }
        if let next = await Self.read(m, folder: folder, id: id) {
            withAnimation(.easeInOut(duration: 0.45)) { loaded = next }
        } else {
            failed = loaded == nil
        }
    }

    @concurrent
    private static func read(_ m: RecreationManifest, folder: URL, id: String) async -> Loaded? {
        let focus = m.focusCenter.flatMap { c in c.count == 3 ? (SIMD3(c[0], c[1], c[2]), m.focusRadius ?? 0.1) : nil }
        let url = folder.appending(path: m.file)
        if m.file.hasSuffix(".usdz") {
            guard let scene = try? SCNScene(url: url) else { return nil }
            return Loaded(id: id, content: .sceneFile(scene), focus: focus, origin: m.origin, closedBottom: m.closedBottom)
        }
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe),
              let (mesh, tex) = try? TexturedPLY.read(data),
              let texName = m.texture ?? tex,
              let src = CGImageSourceCreateWithURL(folder.appending(path: texName) as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
        else { return nil }
        return Loaded(id: id, content: .texturedMesh(mesh, image), focus: focus, origin: m.origin, closedBottom: m.closedBottom)
    }

    private static func marker(_ p: ModelPin) -> RecreationMarker {
        let tone: String = switch p.tone {
        case .danger: "danger"
        case .caution: "caution"
        case .good: "good"
        case .cosmetic: "cosmetic"
        case .setup: "setup"
        default: "neutral"
        }
        return RecreationMarker(id: p.id, number: p.number, point: p.point, rgb: RecreationMarker.rgb(tone: tone), ring: p.area ?? p.ringRadius,
                                label: p.label)
    }
}

/// Keeps the 30° field of view on the view's shorter side, so the framed object fits a tall or a wide hero alike.
private final class FittingSCNView: SCNView {
    override func layoutSubviews() {
        super.layoutSubviews()
        pointOfView?.camera?.projectionDirection = bounds.width < bounds.height ? .horizontal : .vertical
    }
}

private struct RecreationHost: UIViewRepresentable {
    let loaded: RecreationView.Loaded
    let markers: [RecreationMarker]
    let focus: String?
    let spin: Bool
    let animate: Bool
    let active: Bool
    var probe: StageProbe?
    /// The walkthrough's frame: the camera follows its pager instead of `focus`.
    var tour: TourFrame?
    var cue: LeaStageCue?
    let onMarkerTap: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let c = context.coordinator
        c.cueN = cue?.n   // a move from before this view existed is not replayed
        let view = FittingSCNView(frame: .zero)
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling4X
        view.rendersContinuously = false
        view.preferredFramesPerSecond = 120   // draws only while something moves; then at the display's full rate
        view.scene = c.stage.scene
        view.pointOfView = c.stage.cameraNode
        view.allowsCameraControl = true
        let cam = view.defaultCameraController
        cam.interactionMode = .orbitTurntable
        cam.inertiaEnabled = true
        // Turn all the way round, underneath too (the owner, 2026-10-25): an unscanned bottom shows open, and the
        // report offers the bottom scan's own photos (ReportModel.bottomPhotos) when it could not be lined up.
        cam.minimumVerticalAngle = -82
        cam.maximumVerticalAngle = 82
        view.addGestureRecognizer(UITapGestureRecognizer(target: c, action: #selector(Coordinator.tap(_:))))
        let touch = UILongPressGestureRecognizer(target: c, action: #selector(Coordinator.touched(_:)))
        touch.minimumPressDuration = 0
        touch.cancelsTouchesInView = false
        touch.delegate = c
        view.addGestureRecognizer(touch)
        c.view = view
        c.onMarkerTap = onMarkerTap
        c.probe = probe
        c.animate = animate
        let rig = CameraRig(view: view, camera: c.stage.cameraNode)
        rig.onArrive = { [weak c] in c?.landed() }
        rig.onSettle = { [weak c] in c?.settled() }
        c.rig = rig
        view.delegate = c.separator   // numbered markers never cover each other on screen (Report/Stage/MarkerLayout)
        c.stage.set(loaded.content, focus: loaded.focus)
        rig.scale = max(0.02, c.stage.focusRadius)
        c.stage.place(markers, focus: focus)
        c.keepApart()
        cam.target = SCNVector3(c.stage.focusCenter)
        if spin { c.stage.startSpin(); view.isPlaying = active }
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        let c = context.coordinator
        c.onMarkerTap = onMarkerTap
        c.active = active
        c.animate = animate
        if let tour {
            if c.stage.markers != markers { c.stage.place(markers, focus: nil); c.keepApart() }
            c.tour(tour, animate: animate)
            return
        }
        if c.touring {
            // The tour ended: back to the markers as they were, and the camera to the focus (or the whole object).
            c.endTour()
            c.focus = Coordinator.reframe   // never a marker id: the focus branch below frames the camera again
        }
        if c.stage.markers != markers || c.focus != focus {
            c.stage.place(markers, focus: focus)
            c.keepApart()
        }
        if c.focus != focus {
            c.focus = focus
            if let focus {
                c.stage.stopSpin()
                c.flyTo(focus)
            } else {
                c.arrival = nil
                c.stage.light(nil, animated: animate)
                c.goFrame()
            }
        }
        if let cue, c.cueN != cue.n {
            c.cueN = cue.n
            c.lea(cue.command, animate: animate)
        }
        view.isPlaying = (c.stage.isSpinning && active) || (c.rig?.moving ?? false)
    }

    @MainActor
    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        let stage = RecreationStage()
        let separator = MarkerSeparator()
        weak var view: SCNView?

        /// The numbered badges, in rank order, to the separator (the surface dots stay on the true spot).
        func keepApart() {
            let badges = stage.markers.filter { $0.number != nil }.sorted { ($0.number ?? 99) < ($1.number ?? 99) }
            separator.set(badges.compactMap { m in
                stage.markerRoot.childNodes.first { $0.name == "marker:\(m.id)" }.map { ($0, stage.focusRadius * 0.17) }
            })
        }
        var focus: String?
        var onMarkerTap: (String) -> Void = { _ in }
        var rig: CameraRig?
        weak var probe: StageProbe?
        /// Motion on (not Reduce Motion).
        var animate = true
        /// The marker the camera is flying to for a move (its highlight lands on arrival).
        var arrival: String?
        private var touching = false
        private var idle: Task<Void, Never>?

        /// Flies the camera to marker `id` along an arc (the rig); its ring draws on and the part lights up as it lands.
        /// Reduce Motion: a cut, everything at once.
        func flyTo(_ id: String) {
            guard let view, let pose = stage.markerPose(id) else { return }
            idle?.cancel()
            arrival = id
            if animate, let rig {
                stage.prepareArrival(id)
                rig.go(eye: pose.eye, look: pose.look, seconds: Motion.Scene.fly)
            } else {
                stage.move(to: pose.eye, look: pose.look, animated: false)
                view.defaultCameraController.target = SCNVector3(pose.look)
                landed()
                pulse()
            }
        }

        func goFrame() {
            guard let view else { return }
            let p = stage.framePose()
            if animate, let rig { rig.go(eye: p.eye, look: p.look, seconds: Motion.Scene.fly) } else {
                stage.move(to: p.eye, look: p.look, animated: false)
                view.defaultCameraController.target = SCNVector3(p.look)
                pulse()
            }
        }

        /// The move is about 80% there: the highlight lands (ring, surface light, label) and the stage hears of it.
        func landed() {
            guard let id = arrival else { return }
            stage.arrive(id, animated: animate)
            probe?.land(id)
            placeLanded()
        }

        /// Tells the stage where the landed part is on screen (fractions of the view), so the photo card can sit in
        /// the corner farthest from it.
        func placeLanded() {
            guard let id = arrival, let view, view.bounds.width > 0, view.bounds.height > 0,
                  let m = stage.markers.first(where: { $0.id == id }) else { return }
            let world = stage.markerRoot.presentation.simdConvertPosition(stage.onSurface(m.point), to: nil)
            let p = view.projectPoint(SCNVector3(world))
            guard p.z > 0, p.z < 1 else { probe?.place(nil); return }
            probe?.place(CGPoint(x: CGFloat(p.x) / view.bounds.width, y: CGFloat(p.y) / view.bounds.height))
        }

        /// The move settled: keep drawing a moment for the draw-on to finish, then back to on-demand; the idle drift
        /// may resume after a quiet spell.
        func settled() {
            placeLanded()   // where the part really ended up, for the photo card's corner
            pulse(seconds: Motion.Scene.drawOn + 0.35)
            scheduleIdle()
        }

        /// After `Motion.Scene.idleDriftAfter` of nobody touching and Lea silent: the gentle turntable again
        /// (never under Reduce Motion, never during the walkthrough or a move).
        func scheduleIdle() {
            idle?.cancel()
            guard animate else { return }
            idle = Task { [weak self] in
                try? await Task.sleep(for: .seconds(Motion.Scene.idleDriftAfter))
                guard let self, !Task.isCancelled, !self.touching, !self.touring, self.animate,
                      !(self.rig?.moving ?? false), !LeaVoice.shared.speaking else { return }
                self.arrival = nil
                self.probe?.land(nil)
                self.stage.startSpin()
                self.view?.isPlaying = self.active
            }
        }

        // MARK: Walkthrough

        private(set) var touring = false
        /// One pose per step, worked out when the tour starts (the turntable is stopped, so they hold).
        private var tourPoses: [TourPose] = []
        private var tourFocuses: [String?]?
        private var tourCut: Int?
        /// The eased move onto the first card's view: frames that change nothing meanwhile do not cut it short.
        private var glide: (progress: Double, until: Date)?
        static let reframe = "\u{0}reframe"

        /// One frame of the tour: the camera on the pager's path between two steps' views, the rings lit by weight.
        func tour(_ f: TourFrame, animate: Bool) {
            guard let view else { return }
            var glideIn = false
            if !touring {
                touring = true
                stage.stopSpin()
                view.allowsCameraControl = false
                glideIn = animate   // from wherever the camera is to the first card's view, eased
            }
            if tourFocuses != f.focuses {
                tourFocuses = f.focuses
                tourPoses = f.focuses.map(stage.tourPose)
            }
            guard let pose = TourCamera.pose(tourPoses, at: f.progress, around: stage.focusCenter, cut: f.cut) else { return }
            if f.cut {
                if tourCut != f.nearest { tourCut = f.nearest; stage.move(to: pose.eye, look: pose.look, animated: false) }
            } else if glideIn {
                stage.move(to: pose.eye, look: pose.look, animated: true)
                glide = (f.progress, Date().addingTimeInterval(0.75))
            } else if let g = glide, g.until > Date(), g.progress == f.progress {
                // still easing in to the first card
            } else {
                glide = nil
                stage.move(to: pose.eye, look: pose.look, animated: false)
            }
            stage.emphasize(TourCamera.weights(f.focuses, at: f.progress, cut: f.cut))
            view.isPlaying = active
        }

        func endTour() {
            touring = false
            tourFocuses = nil
            tourCut = nil
            view?.allowsCameraControl = true
            stage.place(stage.markers, focus: nil)
            keepApart()
        }

        // MARK: Lea

        /// The last of Lea's moves applied (LeaStageCue.n).
        var cueN: Int?
        /// The side the default framing looks from (RecreationStage.framePose).
        static let front: Float = 0.6

        /// One of Lea's moves: fly to a marker, look from a side (the model turned back to rest first, so "front" is
        /// the default framing's side), turn round, zoom. Reduce Motion: a cut.
        func lea(_ command: StageCommand, animate: Bool) {
            guard let view else { return }
            stage.stopSpin()
            let cam = view.defaultCameraController
            let now = TourPose(eye: stage.cameraNode.simdWorldPosition, look: SIMD3<Float>(cam.target))
            var pose: TourPose?
            var seconds = Motion.Scene.fly
            switch command {
            case .show(let id):
                flyTo(id)
                return
            case .view(let side):
                if side != .around { rest(animate: animate) }
                if side == .reset { arrival = nil; stage.light(nil, animated: animate); probe?.land(nil) }
                pose = StageCamera.pose(side, current: now, center: stage.focusCenter, distance: stage.frameDistance,
                                        front: Self.front) ?? stage.framePose()
                seconds = side == .reset ? Motion.Scene.fly : Motion.Scene.orbit
            case .zoom(let zoomIn):
                pose = StageCamera.zoom(now, zoomIn: zoomIn, minDistance: stage.frameDistance * 0.2,
                                        maxDistance: stage.frameDistance * 2.2)
                seconds = Motion.Scene.zoom
            case .point:
                pose = nil   // the stage turns a point into a marker first; the hero flies with .show
            }
            guard let pose else { return }
            idle?.cancel()
            if animate, let rig {
                rig.go(eye: pose.eye, look: pose.look, seconds: seconds)
            } else {
                stage.move(to: pose.eye, look: pose.look, animated: false)
                cam.target = SCNVector3(pose.look)
                pulse()
            }
        }

        /// The turntable back to rest, with the camera's move.
        private func rest(animate: Bool) {
            SCNTransaction.begin()
            SCNTransaction.animationDuration = animate ? Motion.Scene.fly : 0
            stage.pivot.simdOrientation = simd_quatf(angle: 0, axis: SIMD3(0, 1, 0))
            SCNTransaction.commit()
        }

        /// Whether the hero is on screen (the tour draws continuously only then).
        var active = true

        /// Any touch takes over at once: Lea's move in flight stops where it is, the turntable stops, and the photo
        /// hand-off lets go.
        @objc func touched(_ g: UILongPressGestureRecognizer) {
            switch g.state {
            case .began:
                touching = true
                idle?.cancel()
                if rig?.moving ?? false { rig?.cancel(); probe?.land(nil) }
                if stage.isSpinning { stage.stopSpin() }
                // Back to drawing on demand once nothing runs: a cancelled move never reaches `settled`, so nothing
                // else would stop the full-rate drawing of a still model (the walkthrough draws per frame itself).
                if !touring, !stage.isSpinning, !(rig?.moving ?? false) { view?.isPlaying = false }
            case .ended, .cancelled, .failed:
                touching = false
                scheduleIdle()
            default: break
            }
        }

        /// Camera moves are SCNTransactions: keep drawing for their duration, then back to on-demand.
        func pulse(seconds: Double = Motion.Scene.fly + 0.1) {
            guard let view, !stage.isSpinning else { return }
            view.isPlaying = true
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
                guard let self, !self.stage.isSpinning, !(self.rig?.moving ?? false) else { return }
                self.view?.isPlaying = false
            }
        }

        @objc func tap(_ g: UITapGestureRecognizer) {
            guard let view else { return }
            let hits = view.hitTest(g.location(in: view), options: [.searchMode: SCNHitTestSearchMode.all.rawValue,
                                                                     .rootNode: stage.markerRoot])
            if let id = stage.markerID(atHit: hits.map { ($0.node, SIMD3<Float>($0.localCoordinates)) }) { onMarkerTap(id) }
        }

        nonisolated func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }
}

#if DEBUG
#Preview("Recreation") {
    RecreationView(source: .none, markers: [])
        .frame(height: 360)
        .background(Color.black)
}
#endif
