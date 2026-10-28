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
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    let record: SessionRecord
    /// A picture of the product being scanned, when the App layer has one (see `ProductGhost`). It can also be set
    /// later on the running scan (`scan.ghost`).
    let ghost: ProductGhost?
    @State private var scan: ScanSession?
    @State private var upload: UploadQueue?
    @State private var finished: SessionRecord?
    /// Bumped after a continuation so the report view starts over (it shows "Updating…" until the new report).
    @State private var reportVersion = 0
    @State private var live: LiveEvents?
    /// The product picture of the latest scan, kept for the next one ("Keep scanning", "Scan the bottom").
    @State private var ghostInUse: ProductGhost?

    init(record: SessionRecord, ghost: ProductGhost? = nil) {
        self.record = record
        self.ghost = ghost
    }

    private var current: SessionRecord { finished ?? record }

    var body: some View {
        Group {
            if let scan, scan.continuing {
                ScanView(scan: scan, live: live, close: { leaveContinuation(scan) },
                         keepScanning: { continueScanning(carrying: scan.ghost) }) { r in
                    withAnimation(Motion.reveal) {
                        finished = r
                        self.scan = nil
                        reportVersion += 1
                    }
                }
                .id(ObjectIdentifier(scan))   // a new session starts with a clean review state
                .transition(.opacity)
            } else if finished != nil || record.status != .capturing, let upload {
                ReportView(record: current, upload: upload, close: { dismiss() })
                    .id(reportVersion)
                    .environment(\.continueScan, canContinue ? ContinueScanAction(run: { continueScanning() }) : nil)
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
            } else if let scan {
                ScanView(scan: scan, live: live, close: { dismiss() }, keepScanning: { continueScanning(carrying: scan.ghost) }) { r in
                    withAnimation(Motion.reveal) { finished = r }
                }
                .id(ObjectIdentifier(scan))
                    .environment(\.scanBottom, scan.hasLiDAR ? ScanBottomAction { r in if let r { finished = r; scanBottom(r) } } : nil)
                    .transition(.opacity)
            }
        }
        .background(Color.black.ignoresSafeArea())
        .safeAreaInset(edge: .top) {
            // Voice/ScanTalkBar: talk with Lea while scanning; the conversation carries on at the report.
            if capturing, let gateway = app.gateway, app.isPaired {
                ScanTalkBar(scanID: record.scanID, gateway: gateway, diag: Diag.session(app.storage.dir(record)),
                            sessionID: { [upload] in upload?.state.gatewaySessionID }, live: { liveScan },
                            eyes: eyesForLea(gateway))
                    .padding(.horizontal, Spacing.m)
            }
        }
        .onDisappear { live?.stop() }
        .onAppear {
            guard upload == nil, scan == nil else { return }
            if let gateway = app.gateway, app.isPaired {
                let q = UploadQueue(record: record, storage: app.storage, gateway: gateway)
                if record.status != .capturing { q.enqueueAll(stillIDs: record.stillIDs, scanReady: true) }
                upload = q
                let events = LiveEvents(scanID: record.scanID, corrected: record.objectName, diag: Diag.session(app.storage.dir(record)))
                events.start(gateway: gateway) { [weak q] in q?.state.gatewaySessionID }
                live = events
            }
            guard record.status == .capturing else { return }
            let s = ScanSession(record: record, storage: app.storage, upload: upload, probe: app.stillProbe)
            s.ghost = ghost
            #if DEBUG
            if s.ghost == nil, CommandLine.arguments.contains("--demo-ghost") { s.ghost = .demo }   // gallery / screenshot job
            #endif
            scan = s
        }
    }

    /// Our ARKit scanner lets Lea see: each turn carries the camera's photos (Voice/LeaSight). The scan is read at each
    /// turn (a continued scan is a new ScanSession).
    private func eyesForLea(_ gateway: GatewayClient) -> LeaEyes? {
        guard scan != nil else { return nil }
        return ScanEyes(scan: { self.scan }, gateway: gateway)
    }

    /// A capture is on screen (not the report).
    private var capturing: Bool {
        if let scan, scan.continuing { return true }
        return finished == nil && record.status == .capturing && scan != nil
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
            if let alert = scan.alert { live["alert"] = alert.text }
            if let cue = scan.displayCue { live["on-screen cue"] = cue }
            if scan.relocalizing { live["tracking"] = "lost; finding its place again" }
            if scan.flip != nil { live["part"] = "the bottom: the object is turned over for its underside" }
            if scan.enough { live["enough"] = "yes: they can tap Done" }
            if let circled = scan.sight.circled { live["circled on screen"] = circled }
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

    /// `carrying`: the product picture the scan being left was showing (it may have arrived after the scan began).
    private func continueScanning(carrying picture: ProductGhost? = nil) {
        guard canContinue else { return }
        if let picture { ghostInUse = picture }
        let latest = app.storage.listSessions().first { $0.id == current.id } ?? current
        let s = ScanSession(record: latest, storage: app.storage, upload: upload, probe: app.stillProbe, continuing: true)
        s.ghost = ghostInUse ?? ghost
        scan = s
    }

    /// "Scan the bottom": a continuation that is a quick scan of the flipped object (Capture/Handheld/FlipSegment).
    private func scanBottom(_ r: SessionRecord) {
        let latest = app.storage.listSessions().first { $0.id == r.id } ?? r
        let s = ScanSession(record: latest, storage: app.storage, upload: upload, probe: app.stillProbe, continuing: true)
        s.ghost = ghostInUse ?? ghost
        s.flip = FlipSegmentController(scan: s)
        LeaVoice.shared.dynamicCue(FlipSegmentController.prompt, diag: s.diag)
        withAnimation(Motion.reveal) { scan = s }
    }
}

/// A scan saved on the phone with no server to send it to: what happened and what to do, on the canvas.
struct OfflineView: View {
    let close: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            Spacer()
            Image(systemName: "wifi.slash").font(.system(size: 34, weight: .regular)).foregroundStyle(Palette.secondary)
                .accessibilityHidden(true)
            Text("Saved on the phone").display(.screen).foregroundStyle(Palette.ink)
                .accessibilityAddTraits(.isHeader)
            Text("Pair with the gateway in Settings, then open this scan again to upload it.")
                .font(.body).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button("Close", action: close).buttonStyle(.primary)
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.bottom, Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.canvas.ignoresSafeArea())
    }
}

/// The capture screen: point at the object, tap Scan, walk around while the live point cloud fills in, tap Done.
/// Overlays (Design/INTEGRATION.md): the coverage dial, one cue banner, the lock brackets around the object, a tick
/// per photo, and the backend's live guess of what the object is with a quiet "areas checked" count. Everything
/// here reads coarse session state (≤ 10 changes a second); the close-up target is drawn by the renderer itself,
/// anchored on the object. Done: the cloud gathers into the object, then the report.
struct ScanView: View {
    @ObservedObject var scan: ScanSession
    @ObservedOptional var live: LiveEvents?
    let close: () -> Void
    /// Back to the scanner for the same scan (from the review after Done); nil where that is not possible.
    let keepScanning: (() -> Void)?
    let finished: (SessionRecord) -> Void

    /// The review shown after Done: what was captured and what is wrong with it (`CaptureReviewView`).
    @State private var review: (record: SessionRecord, report: QualityReport)?
    @State private var leadShowing = false
    @State private var ghostOverlay = true
    @State private var doneTick = 0
    @State private var warnTick = 0
    @State private var correcting = false
    @State private var correction = ""
    /// "Turn it" chosen before Scan (Capture/Handheld): starts once the object is locked.
    @State private var turnWanted = false
    @State private var identitySaid = IdentitySpeech.Said()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(scan: ScanSession, live: LiveEvents? = nil, close: @escaping () -> Void, keepScanning: (() -> Void)? = nil,
         finished: @escaping (SessionRecord) -> Void) {
        self.scan = scan
        _live = ObservedOptional(wrappedValue: live)
        self.close = close
        self.keepScanning = keepScanning
        self.finished = finished
    }

    var body: some View {
        ZStack {
            GeometryReader { geo in
                PointCloudView(scan: scan)
                    .onAppear { scan.setViewSize(geo.size) }
                    .onValueChange(of: geo.size) { scan.setViewSize($1) }
                    .gesture(SpatialTapGesture().onEnded { scan.tap(at: $0.location) })
                    // Voice/LeaSight: Lea's circles on the object, and a long press to point at something for her.
                    .overlay { LeaSightOverlay(scan: scan) }
                    .simultaneousGesture(LongPressGesture(minimumDuration: 0.45)
                        .sequenced(before: DragGesture(minimumDistance: 0))
                        .onEnded { value in
                            guard case .second(true, let drag?) = value, let frame = scan.session.currentFrame,
                                  scan.sight.point(at: drag.location, viewSize: geo.size, frame: frame, session: scan.session) else { return }
                            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                            WakeListener.shared.listenForFollowUp()   // point, then ask: "what's this?"
                        })
                    .task {
                        // Continuing a finished scan: its top findings are circled where they are on the object.
                        guard scan.continuing, let json = CachedReport.load(scan.record, storage: scan.storage) else { return }
                        scan.sight.show(findings: ReportModel(json: json).mostLikely.compactMap { p in p.point.map { ($0, p.part) } })
                    }
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
                if let ghost = scan.ghost, ghostOverlay, scan.stage == .scanning, !scan.dome.isDone, let rect = scan.boxRect {
                    GhostOnObject(ghost: ghost, rect: rect)
                }
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
                        coverageIndicator
                            .transition(.opacity)
                    }
                }
                // One cue at a time: an alert beats a warning, a warning beats a lead, a lead beats "keep walking".
                LeadCueBanner(lead: live?.lead, muted: cueWinner != .lead, showing: $leadShowing)   // what the person suspects
                Spacer()
                CueBanner(text: cue.text, symbol: cue.symbol, tone: cue.tone)
                    .opacity(cueWinner == .lead ? 0 : 1)
                    .allowsHitTesting(false)
                // No photo / areas counters here: the shot tick confirms each photo; the report counts the rest.
                HandheldModeChip(scan: scan, wanted: $turnWanted)
                ScanBottomChip(scan: scan)
                button
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .overlay(alignment: .leading) {
                if let ghost = scan.ghost, scan.stage != .finishing {
                    GhostCorner(ghost: ghost, overlayOn: $ghostOverlay)
                }
            }
            .overlay(alignment: .trailing) {
                if scan.lastShot != nil, !scan.enough, scan.stage == .scanning {
                    retakeChip.transition(.opacity)
                }
            }
            .opacity(scan.stage == .finishing ? 0.0 : 1)
            .overlay(alignment: .bottom) {
                if scan.stage == .finishing {
                    CueBanner(text: "Scan saved", symbol: "checkmark", tone: .good)
                        .padding(.bottom, 80)
                        .transition(.opacity)
                }
            }
            if let review {
                CaptureReviewView(report: review.report, onContinue: {
                    if !review.report.problems.isEmpty { scan.recordQualityDecision(uploadedAnyway: true) }
                    finished(review.record)
                }, onKeepScanning: keepScanning.map { keep in {
                    scan.recordQualityDecision(uploadedAnyway: false)
                    finished(review.record)
                    keep()
                } })
                .transition(.opacity)
            }
        }
        .motion(Motion.standard, value: scan.stage)
        .haptic(Haptic.milestone, trigger: scan.milestoneTick)
        .haptic(Haptic.commit, trigger: doneTick)
        .haptic(Haptic.warn, trigger: warnTick)
        .onValueChange(of: cue.tone) { _, tone in if tone == .stop { warnTick += 1 } }
        .onValueChange(of: spokenCue, initial: true) { _, text in LeaVoice.shared.cueChanged(text, diag: scan.diag) }   // Voice/VoiceCues
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
        .onValueChange(of: scan.box != nil) { _, locked in if locked, turnWanted { scan.startHandheld() } }
        .onDisappear {
            scan.stop(); LeaVoice.shared.cueChanged(nil); LeaVoice.shared.dropPriorityCue(); WakeListener.shared.capture(false)
        }
        .onValueChange(of: live?.identity) { _, _ in   // what the backend thinks it is: said once, again only when sharper
            // Worked out again when Lea can say it: the newest guess, nothing once the person has corrected it.
            let live = live, said = identitySaid
            LeaVoice.shared.priorityCue(diag: scan.diag,
                                        next: { said.next(live?.identity, corrected: live?.corrected) },
                                        said: { said.spoke() })
        }
        .onValueChange(of: live?.lead) { _, lead in   // what the person suspects, researched by the backend
            if let lead { LeaVoice.shared.dynamicCue(lead.spoken, diag: scan.diag) }
        }
        .statusBarHidden()
    }

    // MARK: Pieces

    /// The sectors with a photo from the side, for the ring.
    private var seenSectors: Set<Int> {
        Set(scan.dome.covered.filter { $0 / CoverageDome.sectors < CoverageDome.rings - 1 }.map { $0 % CoverageDome.sectors })
    }

    /// The ring of sides seen, with the plain count under it ("5 of 8 sides"; close-ups when the object needs them).
    private var coverageIndicator: some View {
        VStack(alignment: .trailing, spacing: Spacing.xs) {
            SideRing(seen: seenSectors, here: scan.hereSector, next: scan.nextCell.map { $0 % CoverageDome.sectors },
                     done: scan.dome.isDone, count: scan.dome.sideSectors, target: ScanProgress.sideSectors)
            Text(CoverageSummary.sides(scan.dome)).font(.caption2.weight(.semibold)).foregroundStyle(Palette.chalk)
                .padding(.horizontal, 8).padding(.vertical, 3).background { SmokeFill(shape: Capsule()) }
            if let closeUps = CoverageSummary.closeUps(scan.dome) {
                Text(closeUps).font(.caption2.weight(.semibold)).foregroundStyle(Palette.chalk)
                    .padding(.horizontal, 8).padding(.vertical, 3).background { SmokeFill(shape: Capsule()) }
            }
        }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
    }

    /// Retake the last photo: one tap, shown for a few seconds after each photo.
    private var retakeChip: some View {
        Button { scan.retakeLast() } label: {
            Label("Retake", systemImage: "arrow.counterclockwise").cameraChip()
        }
        .buttonStyle(PressableCardStyle())
        .frame(minHeight: 44)
        .accessibilityLabel("Retake the last photo")
        .accessibilityHint("Takes the last photo back so it can be taken again")
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
            .opacity(shown && scan.stage != .finishing ? 1 : 0)
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
                .cameraChip()
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

    /// The session's own cue for the stage (what the capture logic wants said), before alerts and close-up wording.
    private var stageCue: (text: String, symbol: String, tone: CueBanner.Tone) {
        switch scan.stage {
        case .ready: return CueStyle.look(scan.displayCue ?? (scan.flip != nil ? FlipSegmentController.prompt : "Point at the object"))
        case .locking: return CueStyle.look(scan.displayCue ?? "Finding the object…")
        case .scanning:
            if let c = scan.displayCue { return CueStyle.look(c) }
            return scan.dome.isDone ? ("All sides covered", "checkmark", .good) : CueStyle.look(scan.handheld == nil ? "Walk slowly around it" : "Hold it up and turn it slowly")
        case .finishing: return ("Scan saved", "checkmark", .good)
        case .failed(let m): return (m, "xmark", .stop)
        }
    }

    /// What the banner shows: an alert (camera taken, position lost, storage, heat) replaces everything; a close-up
    /// cue names the side to photograph.
    private var cue: (text: String, symbol: String, tone: CueBanner.Tone) {
        if isFailed { return stageCue }
        if let alert = scan.alert { return (alert.text, alert.symbol, alert.tone) }
        var c = stageCue
        if c.text == "Move closer here", let prompt = scan.closeupPrompt { c.text = prompt.sentence }
        return c
    }

    private var isFailed: Bool { if case .failed = scan.stage { return true } else { return false } }

    /// What Lea may say: the session's cue by its own words (never an alert, never the close-up sentence).
    private var spokenCue: String? { scan.alert == nil ? stageCue.text : nil }

    private var cueWinner: CueArbiter.Winner {
        CueArbiter.winner(alert: scan.alert, cueTone: stageCue.tone, leadShowing: leadShowing)
    }

    @ViewBuilder private var button: some View {
        switch scan.stage {
        case .ready, .locking:
            Button(action: scan.beginScan) { Label("Scan", systemImage: Symbol.scan) }
                .buttonStyle(.primary(.light))
                .disabled(scan.stage == .locking)
        case .scanning:
            // The scan has enough: Done settles once (1 → 1.04 → 1) and gains a `next` rim; the milestone haptic fires
            // from the session on the same change. Nothing loops. Reduce Motion: the rim alone.
            Button(action: done) { Label("Done", systemImage: "checkmark") }
                .buttonStyle(.primary(.light))
                .overlay(Capsule().strokeBorder(Palette.next, lineWidth: Line.locked).opacity(scan.enough ? 1 : 0))
                .modifier(SettleOnceModifier(trigger: scan.enough, reduceMotion: reduceMotion))
                .motion(Motion.fade, value: scan.enough)
                .accessibilityHint(scan.enough ? "The scan has enough. Tap to finish." : "")
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
            // Saving failed: stay on the failed screen (its Close button) instead of opening a report for a scan
            // that is not on the phone.
            if case .failed = scan.stage { return }
            let left = PointCloudRenderer.finishDuration + 0.2 - Date().timeIntervalSince(start)
            if left > 0 { try? await Task.sleep(for: .seconds(left)) }
            // Check what was saved before the report is asked for; a good scan shows its counts and one button.
            let report = await scan.qualityReport(for: record)
            withAnimation(Motion.reveal) { review = (record, report) }
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
        ScanSession.findTableCue: ("viewfinder", .guide),
        "Move the phone slowly over the table, then tap the object": ("viewfinder", .caution),
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

/// What Lea says when the backend identifies the object during the scan (the chip shows it too, but a person
/// framing the shot rarely reads it): the category first, then once more when it becomes a named product.
enum IdentitySpeech {
    /// How far Lea has got.
    enum Level: Int, Comparable {
        case nothing, category, product
        static func < (a: Level, b: Level) -> Bool { a.rawValue < b.rawValue }
    }

    /// What to say given what was already SAID (a guess that carried a brand at medium confidence was said as its
    /// category only): nothing once the person has named the object.
    static func line(_ identity: LiveEvents.Identity?, said: Level, corrected: String?) -> (words: String, level: Level)? {
        guard corrected == nil, let identity else { return nil }
        let named = [identity.brand, identity.model].compactMap { $0?.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        if !named.isEmpty, identity.confidence == "high", said < .product {
            return ("It's \(article(named[0])) \(named.joined(separator: " ")).", .product)
        }
        guard said == .nothing else { return nil }   // no flip-flopping between categories
        let thing = identity.category.lowercased()
        return ("This looks like \(article(thing)) \(thing).", .category)
    }

    static func article(_ word: String) -> String {
        guard let c = word.lowercased().first else { return "a" }
        return "aeio".contains(c) ? "an" : "a"
    }

    /// What this scan has said so far: `next` offers the words (LeaVoice asks when it can play them), `spoke` records
    /// them once they start.
    @MainActor final class Said {
        private(set) var level = Level.nothing
        private var offered: Level?

        func next(_ identity: LiveEvents.Identity?, corrected: String?) -> String? {
            let l = IdentitySpeech.line(identity, said: level, corrected: corrected)
            offered = l?.level
            return l?.words
        }

        func spoke() {
            if let offered { level = max(level, offered) }
            offered = nil
        }
    }
}
