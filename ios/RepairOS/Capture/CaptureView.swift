import ARKit
import MetalKit
import SwiftUI

/// "Continue scanning" on a finished scan: the report screen (or the scan list) calls this to reopen the scanner for
/// the same scan. Provided by `SessionFlowView`; nil where continuing is not possible (no LiDAR world map, offline).
struct ContinueScanAction {
    let run: () -> Void
    func callAsFunction() { run() }
}

extension EnvironmentValues {
    @Entry var continueScan: ContinueScanAction? = nil
}

/// One continuous flow for a session: capture and then upload + report. A captured session reopens at its report,
/// and "Continue scanning" goes back to the scanner for the same scan (a new segment), then to a refreshed report.
struct SessionFlowView: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var voice: VoiceCoordinator
    let record: SessionRecord
    @State private var scan: ScanSession?
    @State private var flow: ObjectCaptureFlow?
    @State private var upload: UploadQueue?
    @State private var finished: SessionRecord?
    /// Bumped after a continuation so the report view starts over (it shows "Updating…" until the new report).
    @State private var reportVersion = 0

    private var current: SessionRecord { finished ?? flow?.record ?? record }

    var body: some View {
        Group {
            if let scan, scan.continuing {
                ScanView(scan: scan, close: { leaveContinuation(scan) }) { r in
                    finished = r
                    self.scan = nil
                    reportVersion += 1
                }
            } else if finished != nil || record.status != .capturing, let upload {
                ReportView(record: current, upload: upload, close: { dismiss() })
                    .id(reportVersion)
                    .environment(\.continueScan, canContinue ? ContinueScanAction(run: continueScanning) : nil)
            } else if finished != nil || record.status != .capturing {
                OfflineView(close: { dismiss() })
            } else if let flow {
                ObjectCaptureFlowView(flow: flow, close: { dismiss() }) { finished = flow.record }
            } else if let scan {
                ScanView(scan: scan, close: { dismiss() }) { finished = $0 }
            }
        }
        .onAppear {
            guard upload == nil, flow == nil, scan == nil else { return }
            if let gateway = app.gateway, app.isPaired {
                let q = UploadQueue(record: record, storage: app.storage, gateway: gateway)
                if record.status != .capturing { q.enqueueAll(stillIDs: record.stillIDs, scanReady: true) }
                upload = q
            }
            guard record.status == .capturing else { return }
            if record.engine == .objectCapture && ObjectCaptureFlow.isSupported {
                flow = ObjectCaptureFlow(record: record, storage: app.storage, upload: upload, launchedAt: app.launchedAt)
            } else {
                scan = ScanSession(record: record, storage: app.storage, upload: upload, probe: app.stillProbe)
            }
        }
        .safeAreaInset(edge: .top) { LiveVoiceBar(voice: voice).padding(.horizontal, Spacing.m) }
        .onAppear { voice.scene = { liveScene } }
        .onDisappear { voice.scene = nil }
    }

    /// What Lea is told at each turn while this flow is open: which screen, the scan's stage and cue, progress and
    /// close-ups, so the conversation can follow the scan as it happens.
    private var liveScene: VoiceScene {
        var live: [String: String]
        if let scan, finished == nil || scan.continuing {
            live = ["screen": "scan", "stage": Self.describe(scan.stage), "progress": "\(Int(scan.progress * 100))%",
                    "close-ups taken": "\(scan.stills.count)"]
            if let cue = scan.cue { live["on-screen cue"] = cue }
            if scan.relocalizing { live["tracking"] = "lost; finding its place again" }
        } else if let flow, finished == nil, record.status == .capturing {
            live = ["screen": "scan", "stage": Self.describe(flow.step), "photos": "\(flow.shots)",
                    "close-ups taken": "\(flow.closeupShots)"]
            if let cue = flow.cue { live["on-screen cue"] = cue }
            if flow.lightBlocked { live["light"] = "too dark to continue" }
        } else {
            live = ["screen": "report"]
        }
        return VoiceScene(sessionID: upload?.state.gatewaySessionID ?? UploadQueue.gatewaySessionID(of: current, in: app.storage),
                          live: live)
    }

    private static func describe(_ stage: ScanSession.Stage) -> String {
        switch stage {
        case .ready: "ready: they point at the object and tap it to lock on"
        case .locking: "locking on"
        case .scanning: "scanning: walking around it; photos and close-ups take themselves"
        case .finishing: "saving"
        case .failed(let why): "stopped: \(why)"
        }
    }

    private static func describe(_ step: ObjectCaptureFlow.Step) -> String {
        switch step {
        case .starting: "starting the camera"
        case .ready, .detecting: "framing the object"
        case .capturing: "capturing: walking around it"
        case .closeups: "close-ups: they tap anything that looks damaged or important"
        case .finishing: "saving"
        case .photosOnly: "photos only"
        case .reconstructing(let s): "building the model: \(s)"
        case .done: "done"
        case .failed(let why): "stopped: \(why)"
        }
    }

    /// Our scanner, a finished scan, and its package on the phone.
    private var canContinue: Bool {
        current.engine == .arkit && current.status != .capturing
            && FileManager.default.fileExists(atPath: app.storage.scanPackageURL(current).path)
    }

    /// Leaving "Continue scanning" without Done: photos already taken (and posted) are finished like Done (the scan
    /// is re-posted and completed, so the report includes them); with no new photos nothing was sent and the
    /// segment is simply dropped.
    private func leaveContinuation(_ scan: ScanSession) {
        guard !scan.stills.isEmpty else {
            scan.diag.log("continuation_discarded", ["segment": scan.segment])
            self.scan = nil
            return
        }
        scan.diag.log("continuation_left_finishing", ["segment": scan.segment, "stills": scan.stills.count])
        Task {
            finished = await scan.finish()
            self.scan = nil
            reportVersion += 1
        }
    }

    private func continueScanning() {
        let latest = app.storage.listSessions().first { $0.id == current.id } ?? current
        scan = ScanSession(record: latest, storage: app.storage, upload: upload, probe: app.stillProbe, continuing: true)
    }
}

struct OfflineView: View {
    let close: () -> Void
    var body: some View {
        NavigationStack {
            ContentUnavailableView("Saved on the phone", systemImage: "icloud.slash",
                                   description: Text("Pair with the gateway in Settings, then open this scan again to upload it."))
                .toolbar { Button("Done", action: close) }
        }
    }
}

/// The capture screen: point at the object, tap Scan, walk around while the live point cloud fills in, tap Done.
/// One cue line and a thin progress ring; everything else (lock, photos, torch, upload) is automatic.
struct ScanView: View {
    @Bindable var scan: ScanSession
    let close: () -> Void
    let finished: (SessionRecord) -> Void

    var body: some View {
        ZStack {
            GeometryReader { geo in
                PointCloudView(scan: scan)
                    .onAppear { scan.setViewSize(geo.size) }
                    .onChange(of: geo.size) { scan.setViewSize($1) }
                    .gesture(SpatialTapGesture().onEnded { scan.tap(at: $0.location) })
                    .task {
                        // Developer hook (`--auto-scan`): press Scan once tracking has had a moment (smoke tests).
                        guard CommandLine.arguments.contains("--auto-scan") else { return }
                        try? await Task.sleep(for: .seconds(3))
                        scan.beginScan()
                        // `--auto-done <seconds>`: press Done after that long (measures Done → last upload).
                        let args = CommandLine.arguments
                        if let i = args.firstIndex(of: "--auto-done"), i + 1 < args.count, let secs = Double(args[i + 1]) {
                            // A cancelled sleep returns at once: only press Done if the full time passed.
                            do { try await Task.sleep(for: .seconds(secs)) } catch { return }
                            if scan.stage == .scanning { finished(await scan.finish()) }
                        }
                    }
            }
            .ignoresSafeArea()

            VStack(spacing: 14) {
                HStack {
                    Button(action: { scan.stop(); close() }) {
                        Image(systemName: "xmark").font(.headline).padding(10).background(.ultraThinMaterial, in: Circle())
                    }
                    Spacer()
                    if scan.stage == .scanning {
                        ProgressRing(value: scan.progress)
                    }
                }
                Spacer()
                if let words = words {
                    Text(words)
                        .font(.title3.bold()).foregroundStyle(.white).multilineTextAlignment(.center)
                        .padding(.horizontal, 16).padding(.vertical, 8)
                        .background(.black.opacity(0.6), in: Capsule())
                        .allowsHitTesting(false)
                }
                button
            }
            .padding()
        }
        .sensoryFeedback(.selection, trigger: scan.shotTick)
        .sensoryFeedback(.success, trigger: scan.lockTick)
        .alert("Battery is low", isPresented: .constant(scan.batteryQuestion)) {
            Button("Use the light") { scan.answerBattery(true) }
            Button("No light", role: .cancel) { scan.answerBattery(false) }
        } message: { Text("The light helps in dim rooms but uses battery.") }
        .onAppear { scan.start() }
        .onDisappear { scan.stop() }
        .statusBarHidden()
    }

    private var words: String? {
        switch scan.stage {
        case .ready: scan.cue ?? "Point at the object"
        case .locking: "Finding the object…"
        case .scanning: scan.cue ?? "Walk slowly around it"
        case .finishing: "Saving…"
        case .failed(let m): m
        }
    }

    @ViewBuilder private var button: some View {
        switch scan.stage {
        case .ready, .locking:
            Button(action: scan.beginScan) {
                Text("Scan").font(.title2.bold()).frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
            .disabled(scan.stage == .locking)
        case .scanning:
            Button(action: { Task { finished(await scan.finish()) } }) {
                Text("Done").font(.title2.bold()).frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
        case .failed:
            Button("Close", action: close).buttonStyle(.borderedProminent)
        case .finishing:
            ProgressView().tint(.white)
        }
    }
}

/// The Metal view the renderer draws into (camera image + live LiDAR cloud).
struct PointCloudView: UIViewRepresentable {
    let scan: ScanSession

    func makeUIView(context: Context) -> MTKView {
        let view = MTKView(frame: .zero, device: scan.renderer?.device)
        view.colorPixelFormat = .bgra8Unorm
        view.depthStencilPixelFormat = .depth32Float
        view.preferredFramesPerSecond = 60
        view.delegate = scan.renderer
        return view
    }

    func updateUIView(_ view: MTKView, context: Context) {}
}

/// A thin ring that fills as the scan gathers points and photos.
struct ProgressRing: View {
    let value: Double
    var body: some View {
        ZStack {
            Circle().stroke(.white.opacity(0.25), lineWidth: 3)
            Circle().trim(from: 0, to: value).stroke(.white, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: 30, height: 30)
        .padding(8)
        .background(.ultraThinMaterial, in: Circle())
        .animation(.easeOut, value: value)
    }
}
