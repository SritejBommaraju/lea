import SceneKit
import SwiftUI
import UIKit

// The photo-real recreation for the report's hero: the object (and a little of its table, fading into the stage)
// textured from the scan's own photos, framed to fill the view, soft light, a slow turntable until touched, the
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

    init(source: RecreationSource, markers: [ModelPin], focus: String? = nil, spin: Bool = true,
         onMarkerTap: @escaping (String) -> Void = { _ in }, onLoaded: (() -> Void)? = nil, probe: StageProbe? = nil) {
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var center: RecreationCenter { .shared }

    struct Loaded: @unchecked Sendable {
        var id: String
        var content: RecreationContent
        var focus: (center: SIMD3<Float>, radius: Float)?
        var origin: String
    }

    /// What this build can draw: textured meshes (PLY + atlas, or USDZ). A splat needs the splat renderer.
    nonisolated static func canDraw(_ m: RecreationManifest) -> Bool {
        m.kind == "textured_mesh" && (m.file.hasSuffix(".ply") || m.file.hasSuffix(".usdz"))
    }

    var body: some View {
        ZStack {
            if let loaded {
                RecreationHost(loaded: loaded, markers: markers.map(Self.marker), focus: focus, spin: spin && !reduceMotion,
                               animate: !reduceMotion, active: visible, probe: probe, onMarkerTap: onMarkerTap)
                    .id(loaded.id)
                    .transition(.opacity)
            } else if !failed {
                ProgressView().tint(.white)
            }
        }
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
            return Loaded(id: id, content: .sceneFile(scene), focus: focus, origin: m.origin)
        }
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe),
              let (mesh, tex) = try? TexturedPLY.read(data),
              let texName = m.texture ?? tex,
              let src = CGImageSourceCreateWithURL(folder.appending(path: texName) as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
        else { return nil }
        return Loaded(id: id, content: .texturedMesh(mesh, image), focus: focus, origin: m.origin)
    }

    private static func marker(_ p: ModelPin) -> RecreationMarker {
        let tone: String = switch p.tone {
        case .danger: "danger"
        case .caution: "caution"
        case .good: "good"
        case .cosmetic: "cosmetic"
        default: "neutral"
        }
        return RecreationMarker(id: p.id, number: p.number, point: p.point, rgb: RecreationMarker.rgb(tone: tone), ring: p.area ?? p.ringRadius)
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
    let onMarkerTap: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let c = context.coordinator
        let view = FittingSCNView(frame: .zero)
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling4X
        view.rendersContinuously = false
        view.preferredFramesPerSecond = 60
        view.scene = c.stage.scene
        view.pointOfView = c.stage.cameraNode
        view.allowsCameraControl = true
        let cam = view.defaultCameraController
        cam.interactionMode = .orbitTurntable
        cam.inertiaEnabled = true
        // Stay above the table: the recreation is open underneath.
        cam.minimumVerticalAngle = 4
        cam.maximumVerticalAngle = 82
        view.addGestureRecognizer(UITapGestureRecognizer(target: c, action: #selector(Coordinator.tap(_:))))
        let touch = UILongPressGestureRecognizer(target: c, action: #selector(Coordinator.touched(_:)))
        touch.minimumPressDuration = 0
        touch.cancelsTouchesInView = false
        touch.delegate = c
        view.addGestureRecognizer(touch)
        c.view = view
        c.onMarkerTap = onMarkerTap
        view.delegate = c.separator   // numbered markers never cover each other on screen (Report/Stage/MarkerLayout)
        probe?.resolve = { [weak c] center, radius in c?.resolve(center, radius) }
        c.stage.set(loaded.content, focus: loaded.focus)
        c.stage.place(markers, focus: focus)
        c.keepApart()
        cam.target = SCNVector3(c.stage.focusCenter)
        if spin { c.stage.startSpin(); view.isPlaying = active }
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        let c = context.coordinator
        c.onMarkerTap = onMarkerTap
        if c.stage.markers != markers || c.focus != focus {
            c.stage.place(markers, focus: focus)
            c.keepApart()
        }
        if c.focus != focus {
            c.focus = focus
            if let focus {
                c.stage.stopSpin()
                c.stage.fly(to: focus, animated: animate)
                view.defaultCameraController.target = SCNVector3(c.stage.markers.first { $0.id == focus }.map { c.stage.onSurface($0.point) } ?? c.stage.focusCenter)
            } else {
                c.stage.frame(animated: animate)
                view.defaultCameraController.target = SCNVector3(c.stage.focusCenter)
            }
            c.pulse()
        }
        view.isPlaying = c.stage.isSpinning && active
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

        @objc func touched(_ g: UILongPressGestureRecognizer) {
            guard g.state == .began, stage.isSpinning else { return }
            stage.stopSpin()
            view?.isPlaying = false
        }

        /// Camera moves are SCNTransactions: keep drawing for their duration, then back to on-demand.
        func pulse() {
            guard let view, !stage.isSpinning else { return }
            view.isPlaying = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                guard let self, !self.stage.isSpinning else { return }
                self.view?.isPlaying = false
            }
        }

        /// A circle on screen → the spot on the recreation's surface under its centre (scan frame), and the circle's
        /// radius at that depth. nil when the centre misses the object.
        func resolve(_ center: CGPoint, _ r: CGFloat) -> (point: SIMD3<Float>, radius: Float)? {
            guard let view else { return nil }
            let hits = view.hitTest(center, options: [.searchMode: SCNHitTestSearchMode.closest.rawValue,
                                                      .rootNode: stage.modelNode])
            guard let hit = hits.first else { return nil }
            let world = SIMD3<Float>(hit.worldCoordinates)
            let z = view.projectPoint(hit.worldCoordinates).z
            let edge = SIMD3<Float>(view.unprojectPoint(SCNVector3(Float(center.x + r), Float(center.y), z)))
            // The markers' frame is the scan frame (findings' point_m), so the target uses it too.
            let local = stage.markerRoot.presentation.simdConvertPosition(world, from: nil)
            return (local, max(0.01, simd_distance(world, edge)))
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
