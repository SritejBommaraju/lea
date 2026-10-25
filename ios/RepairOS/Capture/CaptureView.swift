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
    @Entry var liveEvents: LiveEvents? = nil
}

/// One continuous flow for a session: capture and then upload + report. A captured session reopens at its report,
/// and "Continue scanning" goes back to the scanner for the same scan (a new segment), then to a refreshed report.
struct SessionFlowView: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    let record: SessionRecord
    @State private var scan: ScanSession?
    @State private var flow: ObjectCaptureFlow?
    @State private var upload: UploadQueue?
    @State private var finished: SessionRecord?
    /// Bumped after a continuation so the report view starts over (it shows "Updating…" until the new report).
    @State private var reportVersion = 0
    @State private var live: LiveEvents?

    private var current: SessionRecord { finished ?? flow?.record ?? record }

    var body: some View {
        Group {
            if let scan, scan.continuing {
                ScanView(scan: scan, live: live, close: { leaveContinuation(scan) }) { r in
                    withAnimation(Motion.reveal) {
                        finished = r
                        self.scan = nil
                        reportVersion += 1
                    }
                }
                .transition(.opacity)
            } else if finished != nil || record.status != .capturing, let upload {
                ReportView(record: current, upload: upload, close: { dismiss() })
                    .id(reportVersion)
                    .environment(\.continueScan, canContinue ? ContinueScanAction(run: continueScanning) : nil)
                    .environment(\.scanBottom, canContinue ? ScanBottomAction { _ in scanBottom(current) } : nil)   // Capture/Handheld/FlipSegment
                    .environment(\.liveEvents, live)
                    // The Done gather hands over to the same cloud on the report's dark stage: a plain fade, no zoom.
                    .transition(.opacity)
            } else if finished != nil || record.status != .capturing,
                      FileManager.default.fileExists(atPath: CachedReport.url(current, storage: app.storage).path) {
                // Not paired (or no address): the report saved on the phone still opens, offline.
                ReportView(record: current, upload: nil, close: { dismiss() }, offline: true)
            } else if finished != nil || record.status != .capturing {
                OfflineView(close: { dismiss() })
            } else if let flow {
                ObjectCaptureFlowView(flow: flow, close: { dismiss() }) { finished = flow.record }
            } else if let scan {
                ScanView(scan: scan, live: live, close: { dismiss() }) { r in withAnimation(Motion.reveal) { finished = r } }
                    .environment(\.scanBottom, scan.hasLiDAR ? ScanBottomAction { r in if let r { finished = r; scanBottom(r) } } : nil)
                    .transition(.opacity)
            }
        }
        .background(Color.black.ignoresSafeArea())
        .safeAreaInset(edge: .top) {
            // Voice/ScanTalkBar: talk with Lea while scanning; the conversation carries on at the report.
            if capturing, let gateway = app.gateway, app.isPaired {
                ScanTalkBar(scanID: record.scanID, gateway: gateway, diag: Diag.session(app.storage.dir(record)),
                            sessionID: { [upload] in upload?.state.gatewaySessionID }, live: { liveScan })
                    .padding(.horizontal, Spacing.m)
            }
        }
        .onDisappear { live?.stop() }
        .onAppear {
            guard upload == nil, flow == nil, scan == nil else { return }
            if let gateway = app.gateway, app.isPaired {
                let q = UploadQueue(record: record, storage: app.storage, gateway: gateway)
                if record.status != .capturing { q.enqueueAll(stillIDs: record.stillIDs, scanReady: true) }
                upload = q
                let events = LiveEvents(scanID: record.scanID, corrected: record.objectName, diag: Diag.session(app.storage.dir(record)))
                events.start(gateway: gateway) { [weak q] in q?.state.gatewaySessionID }
                live = events
            }
            guard record.status == .capturing else { return }
            if record.engine == .objectCapture && ObjectCaptureFlow.isSupported {
                flow = ObjectCaptureFlow(record: record, storage: app.storage, upload: upload, launchedAt: app.launchedAt)
            } else {
                scan = ScanSession(record: record, storage: app.storage, upload: upload, probe: app.stillProbe)
            }
        }
    }

    /// A capture is on screen (not the report).
    private var capturing: Bool {
        if let scan, scan.continuing { return true }
        return finished == nil && record.status == .capturing && (flow != nil || scan != nil)
    }

    /// What Lea is told at each turn while scanning: the stage, the on-screen cue, progress and close-ups.
    private var liveScan: [String: String] {
        var live = ["screen": "scan"]
        if let scan {
            live["stage"] = switch scan.stage {
            case .ready: "ready: they point at the object and tap it to lock on"
            case .locking: "locking on"
            case .scanning: "scanning: walking around it; photos and close-ups take themselves"
            case .finishing: "saving"
            case .failed(let why): "stopped: \(why)"
            }
            live["progress"] = "\(Int(scan.progress * 100))%"
            live["close-ups taken"] = "\(scan.stills.count)"
            if let cue = scan.cue { live["on-screen cue"] = cue }
            if scan.relocalizing { live["tracking"] = "lost; finding its place again" }
            if scan.flip != nil { live["part"] = "the bottom: the object is turned over for its underside" }
            if scan.enough { live["enough"] = "yes: they can tap Done" }
        } else if let flow {
            live["stage"] = switch flow.step {
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
            live["photos"] = "\(flow.shots)"
            live["close-ups taken"] = "\(flow.closeupShots)"
            if let cue = flow.cue { live["on-screen cue"] = cue }
            if flow.lightBlocked { live["light"] = "too dark to continue" }
        }
        return live
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

    /// "Scan the bottom": a continuation that is a quick scan of the flipped object (Capture/Handheld/FlipSegment).
    private func scanBottom(_ r: SessionRecord) {
        let latest = app.storage.listSessions().first { $0.id == r.id } ?? r
        let s = ScanSession(record: latest, storage: app.storage, upload: upload, probe: app.stillProbe, continuing: true)
        s.flip = FlipSegmentController(scan: s)
        LeaVoice.shared.dynamicCue(FlipSegmentController.prompt, diag: s.diag)
        withAnimation(Motion.reveal) { scan = s }
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
/// Overlays (Design/INTEGRATION.md): the coverage dial, one cue banner, the lock brackets around the object, a tick
/// per photo, and the backend's live guess of what the object is with a quiet "areas checked" count. Everything
/// here reads coarse session state (≤ 10 changes a second); the close-up target is drawn by the renderer itself,
/// anchored on the object. Done: the cloud gathers into the object, then the report.
struct ScanView: View {
    @Bindable var scan: ScanSession
    var live: LiveEvents?
    let close: () -> Void
    let finished: (SessionRecord) -> Void

    @State private var doneTick = 0
    @State private var warnTick = 0
    @State private var correcting = false
    @State private var correction = ""
    /// "Turn it" chosen before Scan (Capture/Handheld): starts once the object is locked.
    @State private var turnWanted = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(scan: ScanSession, live: LiveEvents? = nil, close: @escaping () -> Void, finished: @escaping (SessionRecord) -> Void) {
        self.scan = scan
        self.live = live
        self.close = close
        self.finished = finished
    }

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
                            if scan.stage == .scanning { done() }
                        }
                    }
                brackets(in: geo.size)
            }
            .ignoresSafeArea()

            ShotTick(trigger: scan.shotTick)

            VStack(spacing: Spacing.m) {
                HStack(alignment: .top) {
                    Button(action: { scan.stop(); close() }) { Image(systemName: Symbol.close) }
                        .buttonStyle(RoundIconButtonStyle(onCamera: true))
                        .accessibilityLabel("Close")
                    VoiceMuteButton(onCamera: true)
                    Spacer(minLength: Spacing.s)
                    if scan.stage == .scanning || scan.stage == .locking {
                        identityChip
                        Spacer(minLength: Spacing.s)
                        DomeDial(cells: cells, here: scan.hereSector, done: scan.dome.isDone)
                            .transition(.opacity)
                    }
                }
                LeadCueBanner(lead: live?.lead)   // what the person suspects: where to look closely
                Spacer()
                CueBanner(text: cue.text, symbol: cue.symbol, tone: cue.tone)
                    .allowsHitTesting(false)
                // No photo / areas counters here: the shot tick confirms each photo; the report counts the rest.
                HandheldModeChip(scan: scan, wanted: $turnWanted)
                ScanBottomChip(scan: scan)
                button
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .opacity(scan.stage == .finishing ? 0.0 : 1)
            .overlay(alignment: .bottom) {
                if scan.stage == .finishing {
                    CueBanner(text: "Scan saved", symbol: "checkmark", tone: .good)
                        .padding(.bottom, 80)
                        .transition(.opacity)
                }
            }
        }
        .motion(Motion.standard, value: scan.stage)
        .sensoryFeedback(Haptic.milestone, trigger: scan.milestoneTick)
        .sensoryFeedback(Haptic.commit, trigger: doneTick)
        .sensoryFeedback(Haptic.warn, trigger: warnTick)
        .onChange(of: cue.tone) { _, tone in if tone == .stop { warnTick += 1 } }
        .onChange(of: cue.text, initial: true) { _, text in LeaVoice.shared.cueChanged(text, diag: scan.diag) }   // Voice/VoiceCues
        .alert("Battery is low", isPresented: .constant(scan.batteryQuestion)) {
            Button("Use the light") { scan.answerBattery(true) }
            Button("No light", role: .cancel) { scan.answerBattery(false) }
        } message: { Text("The light helps in dim rooms but uses battery.") }
        .alert("What is it?", isPresented: $correcting) {
            TextField("Name it in a few words", text: $correction)
            Button("Save") { live?.correct(correction); scan.nameObject(correction) }
            Button("Cancel", role: .cancel) { }
        } message: { Text("Helps the report. The scan carries on.") }
        .task { await VoiceProbe.run(scan: scan) }   // `--voice-frame-probe` only
        .onAppear { scan.start(); WakeListener.shared.capture(true) }
        .onChange(of: scan.box != nil) { _, locked in if locked, turnWanted { scan.startHandheld() } }
        .onDisappear { scan.stop(); LeaVoice.shared.cueChanged(nil); WakeListener.shared.capture(false) }
        .onChange(of: live?.lead) { _, lead in   // what the person suspects, researched by the backend
            if let lead { LeaVoice.shared.dynamicCue(lead.spoken, diag: scan.diag) }
        }
        .statusBarHidden()
    }

    // MARK: Pieces

    private var cells: [CellState] {
        (0..<CoverageDome.cellCount).map { scan.dome.isCovered($0) ? .covered : ($0 == scan.nextCell ? .next : .missing) }
    }

    /// The lock brackets: centred while searching, then on the object's projected box (≤ 10 Hz, eased between).
    @ViewBuilder private func brackets(in size: CGSize) -> some View {
        let locked = scan.box != nil
        let searching = scan.stage == .ready || scan.stage == .locking
        let rect = locked ? scan.boxRect : nil
        let shown = searching || rect != nil
        let r = rect ?? CGRect(x: size.width / 2 - 110, y: size.height / 2 - 110, width: 220, height: 220)
        LockBrackets(phase: locked ? .locked : .searching)
            .frame(width: r.width, height: r.height)
            .position(x: r.midX, y: r.midY)
            .opacity(shown && scan.stage != .finishing ? (locked ? 0.85 : 0.7) : 0)
            .animation(reduceMotion ? nil : .linear(duration: 0.1), value: rect)
            .animation(Motion.fade, value: shown)
            .allowsHitTesting(false)
    }

    /// What the backend thinks the object is (live `identified`); tap to correct it.
    @ViewBuilder private var identityChip: some View {
        if let words = live?.corrected ?? live?.identity?.words {
            Button { correction = live?.corrected ?? ""; correcting = true } label: {
                HStack(spacing: 6) {
                    Image(systemName: live?.corrected == nil ? "sparkle.magnifyingglass" : "checkmark")
                        .imageScale(.small)
                    Text(live?.corrected == nil ? "Looks like: \(words)" : words).lineLimit(1)
                }
                .font(.pillLabel)
                .foregroundStyle(.white)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Color.black.opacity(0.5), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            }
            .buttonStyle(PressableCardStyle())
            .accessibilityHint("Tap to correct it")
            .transition(.opacity.combined(with: .scale(scale: 0.9)))
            .padding(.top, 6)
        }
    }

    private var counters: some View {
        HStack(spacing: Spacing.s) {
            if !scan.stills.isEmpty {
                StatusPill(text: scan.stills.count == 1 ? "1 photo" : "\(scan.stills.count) photos", symbol: "camera.fill", onCamera: true)
            }
            if let n = live?.areasChecked, n > 0 {
                StatusPill(text: n == 1 ? "1 area checked" : "\(n) areas checked", symbol: "checkmark.circle", onCamera: true)
                    .transition(.opacity)
            }
        }
        .motion(Motion.fade, value: live?.areasChecked)
        .contentTransition(.numericText())
    }

    private var cue: (text: String, symbol: String, tone: CueBanner.Tone) {
        switch scan.stage {
        case .ready: return CueStyle.look(scan.cue ?? (scan.flip != nil ? FlipSegmentController.prompt : "Point at the object"))
        case .locking: return CueStyle.look(scan.cue ?? "Finding the object…")
        case .scanning:
            if let c = scan.cue { return CueStyle.look(c) }
            return scan.dome.isDone ? ("All sides covered", "checkmark", .good) : CueStyle.look(scan.handheld == nil ? "Walk slowly around it" : "Hold it up and turn it slowly")
        case .finishing: return ("Scan saved", "checkmark", .good)
        case .failed(let m): return (m, "xmark", .stop)
        }
    }

    @ViewBuilder private var button: some View {
        switch scan.stage {
        case .ready, .locking:
            Button(action: scan.beginScan) { Label("Scan", systemImage: Symbol.scan) }
                .buttonStyle(.primary(.light))
                .disabled(scan.stage == .locking)
        case .scanning:
            Button(action: done) { Label("Done", systemImage: "checkmark") }
                .symbolEffect(.pulse, options: .repeating, isActive: scan.enough)   // the scan has enough: tap Done
                .scaleEffect(scan.enough ? 1.04 : 1)
                .animation(scan.enough ? .easeInOut(duration: 0.6).repeatForever(autoreverses: true) : .default, value: scan.enough)
                .buttonStyle(.primary(.light))
        case .failed:
            Button("Close", action: close).buttonStyle(.primary(.light))
        case .finishing:
            Color.clear.frame(height: 56)
        }
    }

    /// Done: the cloud gathers into the object while the scan saves; the report comes when both are finished.
    private func done() {
        guard scan.stage == .scanning else { return }
        doneTick += 1
        let start = Date()
        Task {
            let record = await scan.finish()
            let left = PointCloudRenderer.finishDuration + 0.2 - Date().timeIntervalSince(start)
            if left > 0 { try? await Task.sleep(for: .seconds(left)) }
            finished(record)
        }
    }
}

/// The symbol and urgency for each capture cue (the session's own words; INTEGRATION.md's symbols).
enum CueStyle {
    static func look(_ text: String) -> (text: String, symbol: String, tone: CueBanner.Tone) {
        let (symbol, tone) = table[text] ?? HandheldCues.table[text] ?? ("figure.walk", .guide)
        return (text, symbol, tone)
    }

    static let table: [String: (String, CueBanner.Tone)] = [
        "Point at the object": (Symbol.scan, .guide),
        "Finding the object…": (Symbol.lock, .guide),
        "Finding where you left off…": ("arrow.triangle.2.circlepath", .guide),
        "Point at the object to pick up where you left off": ("arrow.triangle.2.circlepath", .guide),
        "Walk slowly around it": ("figure.walk", .guide),
        "Keep walking around it": ("figure.walk", .guide),
        "Move closer here": ("plus.magnifyingglass", .guide),
        ScanSession.enoughCue: ("checkmark", .good),
        "Hold still a moment": ("hand.raised.fill", .guide),
        "Tap the object to focus on it": ("hand.tap.fill", .guide),
        "Too dark: add light": ("lightbulb.fill", .caution),
        "Add light or move closer": ("lightbulb.fill", .caution),
        "Keep the object in frame": (Symbol.scan, .caution),
        "Too far: move closer": ("arrow.up.forward.and.arrow.down.backward", .caution),
        "Too close: back up": ("arrow.down.backward.and.arrow.up.forward", .stop),
        "Blurry: hold still": ("tortoise.fill", .caution),
        "Move slowly": ("tortoise.fill", .caution),
    ]
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
