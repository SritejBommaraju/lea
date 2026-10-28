import SceneKit
import SwiftUI

// Demo replay (hidden in Settings): plays one real saved scan from this phone and its saved report, fully offline,
// for when the venue network fails. Nothing is made up: the cloud is the scan's own PLY, the dial fills from its own
// photos' coverage cells in the order they were taken, the report is the cached report JSON, the photos are the
// crops saved beside it, and "Show on object" relocalises with the scan's own world map.

@MainActor
enum DemoReplay {
    static let enabledKey = "demoReplay"
    static let unlockedKey = "demoReplayUnlocked"
    static let sessionKey = "demoReplaySession"

    /// A scan that can be replayed: a cached report and a point cloud on the phone.
    static func eligible(_ r: SessionRecord, storage: ScanStorage) -> Bool {
        FileManager.default.fileExists(atPath: CachedReport.url(r, storage: storage).path)
            && ModelSource.cloudFallback(in: storage.scanDir(r)) != nil
    }

    static func chosen(app: AppState) -> SessionRecord? {
        guard UserDefaults.standard.bool(forKey: enabledKey) else { return nil }
        let id = UserDefaults.standard.string(forKey: sessionKey)
        return app.sessions.first { $0.id == id && eligible($0, storage: app.storage) }
    }

    /// Crops of the cached report: (saved on the phone, total).
    static func cropStatus(_ r: SessionRecord, storage: ScanStorage) -> (saved: Int, total: Int) {
        guard let json = CachedReport.load(r, storage: storage) else { return (0, 0) }
        let shas = ReportModel(json: json).cropSHAs
        let dir = CropSource.dir(storage.dir(r))
        return (shas.filter { FileManager.default.fileExists(atPath: dir.appending(path: $0).path) }.count, shas.count)
    }

    /// Fetches the report's crops from the gateway into the session folder (once, while online).
    static func saveCrops(_ r: SessionRecord, storage: ScanStorage, gateway: GatewayClient) async -> Int {
        guard let json = CachedReport.load(r, storage: storage) else { return 0 }
        let source = CropSource.saved(in: CropSource.dir(storage.dir(r)), fallback: .gateway(gateway))
        var n = 0
        for sha in ReportModel(json: json).cropSHAs where await source.fetch(sha) != nil { n += 1 }
        return n
    }
}

/// The replay: the saved scan builds up around the object, the cloud gathers, then the saved report (offline).
struct DemoReplayFlowView: View {
    let record: SessionRecord
    @Environment(\.dismiss) private var dismiss
    @State private var showReport = false

    var body: some View {
        Group {
            if showReport {
                ReportView(record: record, upload: nil, close: { dismiss() }, offline: true)
                    .transition(.asymmetric(insertion: .opacity.combined(with: .scale(scale: 1.04)), removal: .opacity))
            } else {
                ScanReplayView(record: record, close: { dismiss() }) { withAnimation(Motion.reveal) { showReport = true } }
                    .transition(.opacity)
            }
        }
        .background(Color.black.ignoresSafeArea())
    }
}

/// The saved cloud appearing sector by sector around the object, the dial filling from the saved photos' cells.
struct ScanReplayView: View {
    let record: SessionRecord
    let close: () -> Void
    let finished: () -> Void
    @EnvironmentObject private var app: AppState
    @State private var cells: [CellState] = Array(repeating: .missing, count: CoverageDome.cellCount)
    @State private var photos = 0
    @State private var shot = 0
    @State private var cloud: ScanCloud?
    @State private var phase = 0     // 0 loading, 1 building, 2 gathering
    @State private var failed = false

    static let buildSeconds = 4.0

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let cloud {
                ReplayScene(cloud: cloud, phase: phase)
                    .ignoresSafeArea()
                    .transition(.opacity)
            }
            ShotTick(trigger: shot, haptic: false)
            VStack(spacing: Spacing.m) {
                HStack(alignment: .top) {
                    Button(action: close) { Image(systemName: Symbol.close) }
                        .buttonStyle(RoundIconButtonStyle(onCamera: true))
                        .accessibilityLabel("Close")
                    Spacer()
                    StatusPill(text: "Replay", symbol: "play.fill", onCamera: true).padding(.top, 12)
                    Spacer()
                    DomeDial(cells: cells, done: phase == 2)
                }
                Spacer()
                CueBanner(text: failed ? "This saved scan can't be read" : phase == 2 ? "Scan saved" : "Replaying a saved scan",
                          symbol: failed ? "xmark" : phase == 2 ? "checkmark" : "play.fill", tone: failed ? .stop : phase == 2 ? .good : .guide)
                if photos > 0 {
                    StatusPill(text: photos == 1 ? "1 photo" : "\(photos) photos", symbol: "camera.fill", onCamera: true)
                        .contentTransition(.numericText())
                }
                Button { finished() } label: { Label("Show the report", systemImage: "doc.text.magnifyingglass") }
                    .buttonStyle(.primary(.light))
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
        .motion(Motion.standard, value: phase)
        .haptic(Haptic.milestone, trigger: phase == 2)
        .statusBarHidden()
        .task { await play() }
    }

    private func play() async {
        let dir = app.storage.scanDir(record)
        guard let url = ModelSource.cloudFallback(in: dir),
              let data = try? Data(contentsOf: url, options: .mappedIfSafe),
              let c = try? PLYReader.read(data, maxPoints: 150_000) else { failed = true; return }
        withAnimation(Motion.fade) { cloud = c }
        phase = 1
        // The scan's own photos, in the order taken, with the coverage cell each one filled.
        let stills: [String?] = record.stillIDs.map { id in
            (try? Data(contentsOf: app.storage.stillURL(record, id)))
                .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                .flatMap { $0["view_cell"] as? String }
        }
        let step = Self.buildSeconds / Double(max(1, stills.count))
        for cell in stills {
            try? await Task.sleep(for: .seconds(step))
            if Task.isCancelled { return }
            photos += 1
            shot += 1
            if let cell, let i = Self.cellIndex(cell) { cells[i] = .covered }
        }
        if stills.isEmpty { try? await Task.sleep(for: .seconds(Self.buildSeconds)) }
        phase = 2
        try? await Task.sleep(for: .seconds(1.3))
        if !Task.isCancelled { finished() }
    }

    /// "r1s07" → ring 1 × 12 + sector 7 (CoverageDome.id).
    static func cellIndex(_ id: String) -> Int? {
        guard id.count == 5, id.hasPrefix("r"), let ring = Int(id.dropFirst().prefix(1)), let sector = Int(id.suffix(2)),
              (0..<CoverageDome.rings).contains(ring), (0..<CoverageDome.sectors).contains(sector) else { return nil }
        return ring * CoverageDome.sectors + sector
    }
}

/// SceneKit: the cloud split into 12 azimuth sectors around the object that fade in one after another (as if walked
/// around) while the camera turns slowly; on `phase` 2 everything gathers inward. Renders only while animating.
private struct ReplayScene: UIViewRepresentable {
    let cloud: ScanCloud
    let phase: Int

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView(frame: .zero)
        view.backgroundColor = .black
        view.antialiasingMode = .multisampling2X
        view.preferredFramesPerSecond = 60
        view.scene = SCNScene()
        view.isPlaying = true
        context.coordinator.build(in: view, cloud: cloud)
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        if phase == 2 { context.coordinator.gather() }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    @MainActor
    final class Coordinator {
        private let pivot = SCNNode()
        private var sectors: [SCNNode] = []
        private var gathered = false

        func build(in view: SCNView, cloud: ScanCloud) {
            guard let scene = view.scene else { return }
            // Frame the bulk of the cloud (median centre, 80th-percentile reach), so stray points never shrink it.
            let (center, radius) = cloud.bulk
            // Points by sector (azimuth around the object), each its own node.
            var buckets = Array(repeating: ([SIMD3<Float>](), [SIMD3<Float>]()), count: CoverageDome.sectors)
            for (i, p) in cloud.positions.enumerated() {
                let s = CoverageDome.sector(azimuth: CoverageDome.azimuth(p - center))
                buckets[s].0.append(p - center)
                buckets[s].1.append(cloud.colors?[i] ?? SIMD3(repeating: 0.8))
            }
            let content = SCNNode()
            for (k, b) in buckets.enumerated() where !b.0.isEmpty {
                let node = SCNNode(geometry: Self.points(b.0, b.1))
                node.opacity = 0
                node.runAction(.sequence([.wait(duration: Double(k) * ScanReplayView.buildSeconds / Double(CoverageDome.sectors)),
                                          .fadeIn(duration: 0.5)]))
                content.addChildNode(node)
                sectors.append(node)
            }
            pivot.addChildNode(content)
            scene.rootNode.addChildNode(pivot)
            pivot.runAction(.repeatForever(.rotateBy(x: 0, y: -2 * .pi, z: 0, duration: 24)))
            let camera = SCNCamera()
            camera.fieldOfView = 46
            camera.projectionDirection = .horizontal    // portrait screen: fit the object's width
            camera.zNear = 0.001
            camera.zFar = 50
            let cam = SCNNode()
            cam.camera = camera
            cam.simdPosition = simd_normalize(SIMD3<Float>(0, 0.55, 1)) * radius * 2.4
            cam.simdLook(at: .zero, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
            scene.rootNode.addChildNode(cam)
            view.pointOfView = cam
        }

        func gather() {
            guard !gathered else { return }
            gathered = true
            let ease = SCNAction.scale(to: 0.82, duration: 0.9)
            ease.timingMode = .easeIn
            pivot.runAction(.group([ease, .sequence([.wait(duration: 0.5), .fadeOpacity(to: 0.85, duration: 0.4)])]))
        }

        private static func points(_ p: [SIMD3<Float>], _ c: [SIMD3<Float>]) -> SCNGeometry {
            let n = p.count
            func source(_ v: [SIMD3<Float>], _ s: SCNGeometrySource.Semantic) -> SCNGeometrySource {
                SCNGeometrySource(data: v.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: s, vectorCount: n,
                                  usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                  dataStride: MemoryLayout<SIMD3<Float>>.stride)
            }
            let element = SCNGeometryElement(data: Array(0..<UInt32(n)).withUnsafeBufferPointer { Data(buffer: $0) },
                                             primitiveType: .point, primitiveCount: n, bytesPerIndex: 4)
            element.pointSize = 3
            element.minimumPointScreenSpaceRadius = 1.2
            element.maximumPointScreenSpaceRadius = 3.5
            let g = SCNGeometry(sources: [source(p, .vertex), source(c, .color)], elements: [element])
            let m = SCNMaterial()
            m.lightingModel = .constant
            m.diffuse.contents = UIColor.white
            g.materials = [m]
            return g
        }
    }
}

/// Settings: the hidden "Demo replay" section (tap the version line five times to show it).
struct DemoReplaySettings: View {
    @EnvironmentObject private var app: AppState
    @AppStorage(DemoReplay.enabledKey) private var enabled = false
    @AppStorage(DemoReplay.sessionKey) private var sessionID = ""
    @State private var saving = false
    @State private var status: (saved: Int, total: Int) = (0, 0)

    /// Newest first.
    private var choices: [SessionRecord] {
        app.sessions.filter { DemoReplay.eligible($0, storage: app.storage) }.sorted { $0.createdAt > $1.createdAt }
    }
    private var chosen: SessionRecord? { choices.first { $0.id == sessionID } }

    var body: some View {
        Section {
            Toggle("Demo replay", isOn: $enabled)
            if enabled {
                Picker("Scan", selection: $sessionID) {
                    Text("Choose a scan").tag("")
                    ForEach(choices) { r in Text(label(r)).tag(r.id) }
                }
                if let chosen {
                    LabeledContent("Photos saved for offline", value: "\(status.saved) of \(status.total)")
                    Button {
                        Task {
                            guard let gateway = app.gateway else { return }
                            saving = true
                            _ = await DemoReplay.saveCrops(chosen, storage: app.storage, gateway: gateway)
                            status = DemoReplay.cropStatus(chosen, storage: app.storage)
                            saving = false
                        }
                    } label: {
                        HStack { Text("Save photos now"); Spacer(); if saving { ProgressView() } }
                    }
                    .disabled(saving || app.gateway == nil || status.saved == status.total)
                }
            }
        } header: { Text("Demo") } footer: {
            Text("Plays a saved scan and its report from this phone with no network: a Replay button appears on the home screen. Save the photos while the server is reachable.")
        }
        .task(id: sessionID) {
            if sessionID.isEmpty, let first = choices.first { sessionID = first.id }
            if let chosen { status = DemoReplay.cropStatus(chosen, storage: app.storage) }
        }
    }

    private func label(_ r: SessionRecord) -> String {
        let words = CachedReport.load(r, storage: app.storage).flatMap { ReportModel(json: $0).objectWords }
        return [words, r.createdAt.formatted(.dateTime.month(.abbreviated).day().hour().minute())].compactMap { $0 }.joined(separator: " · ")
    }
}
