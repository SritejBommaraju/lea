import AVFoundation
import SwiftUI

/// Home: what lea does, the optional symptom line and one Start button within thumb reach (launch → scanning in
/// two taps: Start, then lock on), plus past scans. First run asks for consent, then offers pairing once; pairing
/// can be skipped to scan offline.
struct RootView: View {
    @Environment(AppState.self) private var app
    @State private var symptom = ""
    @State private var active: SessionRecord?
    @State private var showSettings = false
    @State private var showPairing = false
    @State private var showConsent = false
    @State private var scanAfterConsent = false
    @State private var error: String?
    @State private var askCamera = false
    @State private var starts = 0
    @State private var showAll = false
    @State private var pendingDelete: SessionRecord?
    @State private var replay: SessionRecord?
    /// "Hey Lea" (or the Talk button) on the home screen: Voice/HomeConversationSheet.
    @State private var talking = false
    @State private var talksSeen = 0
    @AppStorage(DemoReplay.enabledKey) private var demoReplay = false
    @AppStorage(DemoReplay.sessionKey) private var demoSession = ""
    private static let recentCount = 3
    @FocusState private var symptomFocused: Bool

    /// The `--autostart-scan` developer flag skips the consent screen so USB smoke tests run unattended.
    private var devAutostart: Bool { CommandLine.arguments.contains("--autostart-scan") }

    var body: some View {
        NavigationStack {
            List {
                HomeHeader(engine: app.capabilities.engine, paired: app.isPaired,
                           settings: { showSettings = true }, pair: { showPairing = true })
                    .plainRow(top: Spacing.s)
                if app.sessions.isEmpty {
                    HowItWorks().plainRow(top: Spacing.xl)
                } else {
                    SectionHeader(title: "Recent scans", count: app.sessions.count) {
                        if app.sessions.count > Self.recentCount {
                            Button("See all") { showAll = true }.font(.subheadline.weight(.semibold))
                        }
                    }
                    .plainRow(top: Spacing.xl, bottom: Spacing.xs)
                    ForEach(app.sessions.prefix(Self.recentCount)) { s in
                        ScanRowButton(record: s, open: { active = $0 }, delete: { pendingDelete = s })
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .background(Palette.canvas.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) { startBar }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(isPresented: $showAll) {
                ScansView(open: { active = $0 }).toolbar(.visible, for: .navigationBar)
            }
            .confirmDeleteScan($pendingDelete)
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(isPresented: $showPairing) { PairingView() }
            .fullScreenCover(isPresented: $showConsent) {
                ConsentView { agreed in
                    showConsent = false
                    if agreed { afterConsent() }
                }
            }
            .sheet(isPresented: $askCamera) {
                CameraPermissionView { granted in
                    askCamera = false
                    if granted { newScan() }
                }
            }
            .fullScreenCover(item: $active, onDismiss: app.reloadSessions) { record in
                SessionFlowView(record: record)
            }
            .fullScreenCover(item: $replay) { record in DemoReplayFlowView(record: record) }
            .alert("Can't start", isPresented: .constant(error != nil)) {
                Button("OK") { error = nil }
            } message: { Text(error ?? "") }
            .task {
                try? await Task.sleep(for: .milliseconds(300))
                if app.autostart, active == nil { newScan() }
                if let id = app.openSessionID, active == nil {
                    app.openSessionID = nil
                    let r = app.sessions.first { $0.id == id }
                    if CommandLine.arguments.contains("--demo-replay") { replay = r } else { active = r }
                }
            }
            // "Hey Siri, scan with Lea" (Voice/ScanIntent): start a scan with the spoken description.
            .onChange(of: IntentInbox.shared.requests, initial: true) { scanFromIntent() }
            // "Hey Siri, hey lea" (Voice/ScanIntent): the conversation opens listening, and Lea asks what's up.
            .onChange(of: IntentInbox.shared.talks, initial: true) { _, talks in
                guard talks > talksSeen else { return }
                talksSeen = talks
                talkToLea()
            }
            .onChange(of: WakeListener.shared.wakes) { if active == nil, replay == nil, app.gateway != nil { talking = true } }
            .sheet(isPresented: $talking) {
                if let gateway = app.gateway {
                    HomeConversationSheet(gateway: gateway) { said in
                        symptom = said
                        newScan()
                    }
                }
            }
            .onAppear {
                if !Consent.accepted && !devAutostart {
                    showConsent = true
                } else {
                    offerPairingOnce()
                }
            }
        }
        .tint(Palette.accent)
    }

    private var startBar: some View {
        VStack(spacing: Spacing.m) {
            if app.isPaired, app.gateway != nil {
                Label("Swipe up to talk to Lea", systemImage: "chevron.up")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    .accessibilityHidden(true)
            }
            FieldWell(symbol: "text.bubble") {
                TextField("What happened? (optional)", text: $symptom)
                    .focused($symptomFocused)
                    .submitLabel(.go)
                    .onSubmit(newScan)
                StoryMicButton(text: $symptom, gateway: app.isPaired ? app.gateway : nil)   // Voice/: hold to dictate
            }
            HStack(spacing: Spacing.s) {
                Button(action: newScan) { Label("Start scan", systemImage: Symbol.scan) }
                    .buttonStyle(.primary)
                    .sensoryFeedback(Haptic.commit, trigger: starts)
                if app.isPaired, app.gateway != nil {
                    Button { talking = true } label: { Image(systemName: "waveform") }
                        .buttonStyle(RoundIconButtonStyle())
                        .accessibilityLabel("Talk to Lea")
                    WakeIndicator()
                }
            }
            if !app.isPaired {
                // The only plumbing on Home, and only when it blocks a report.
                Button { showPairing = true } label: {
                    Text("Not connected · Set up").font(.footnote.weight(.semibold)).foregroundStyle(Palette.cautionText)
                }
                .frame(minHeight: 32)
            }
            if demoReplay, !demoSession.isEmpty, let saved = DemoReplay.chosen(app: app) {
                // Demo only: a quiet text button, never as heavy as Start scan.
                Button { replay = saved } label: { Label("Replay saved scan", systemImage: "play.fill") }
                    .font(.subheadline.weight(.semibold))
                    .frame(minHeight: 32)
            }
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.top, Spacing.m)
        .padding(.bottom, Spacing.s)
        .background {
            VStack(spacing: 0) {
                LinearGradient(colors: [Palette.canvas.opacity(0), Palette.canvas], startPoint: .top, endPoint: .bottom)
                    .frame(height: Spacing.xl)
                Palette.canvas
            }
            .padding(.top, -Spacing.xl)
            .ignoresSafeArea(edges: .bottom)
            .allowsHitTesting(false)
        }
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 20).onEnded { if $0.translation.height < -30 { talkToLea() } })
        .accessibilityAction(named: "Talk to Lea") { talkToLea() }
    }

    /// Swipe up on the start bar: Lea's conversation opens already listening, hands-free, and keeps listening after
    /// each answer (no "Hey Lea" needed; the ear button turns that off).
    private func talkToLea() {
        guard app.isPaired, app.gateway != nil, active == nil else { return }
        WakeListener.shared.continuous = true
        WakeListener.shared.listenForFollowUp()
        talking = true
    }

    private func scanFromIntent() {
        guard active == nil, replay == nil, let story = IntentInbox.shared.take() else { return }
        symptom = story
        newScan()
    }

    private func afterConsent() {
        if scanAfterConsent {
            scanAfterConsent = false
            newScan()
        } else {
            offerPairingOnce()
        }
    }

    private func offerPairingOnce() {
        if !app.isPaired && !UserDefaults.standard.bool(forKey: "pairingOffered") {
            UserDefaults.standard.set(true, forKey: "pairingOffered")
            showPairing = true
        }
    }

    private func newScan() {
        starts += 1
        symptomFocused = false
        guard Consent.accepted || devAutostart else {
            scanAfterConsent = true
            showConsent = true
            return
        }
        guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
            askCamera = true
            return
        }
        guard app.freeBytes >= AppState.minimumFreeBytes else {
            error = "Free up at least 1 GB on the phone first (\(app.freeBytes >> 20) MB free)."
            return
        }
        do {
            // After talking with Lea first (Voice/PreScan): the same gateway session and scan id carry on.
            active = try PreScan.shared.takeRecord(storage: app.storage, symptom: symptom, engine: app.capabilities.engine)
                ?? app.storage.newSession(symptom: symptom, engine: app.capabilities.engine)
            symptom = ""
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private extension View {
    /// A full-bleed list row with the screen gutter and no separator or background.
    func plainRow(top: CGFloat = 0, bottom: CGFloat = 0) -> some View {
        listRowInsets(EdgeInsets(top: top, leading: Spacing.gutter, bottom: bottom, trailing: Spacing.gutter))
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
    }
}

// MARK: - Header

private struct HomeHeader: View {
    let engine: EngineKind
    let paired: Bool
    let settings: () -> Void
    let pair: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            HStack {
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text("lea").font(.wordmark)
                    Circle().fill(Palette.accent).frame(width: 7, height: 7)
                }
                .accessibilityElement()
                .accessibilityLabel("lea")
                .accessibilityAddTraits(.isHeader)
                Spacer()
                Button(action: settings) { Image(systemName: "gearshape") }
                    .buttonStyle(RoundIconButtonStyle())
                    .accessibilityLabel("Settings")
            }
            DomeDial(cells: HomeHeader.art.enumerated().map { $0.offset % 12 < sectorsShown ? $0.element : .missing }, size: 132, style: .app)
                .frame(maxWidth: .infinity)
                .padding(.vertical, Spacing.s)
                .task { await drawIn() }
            VStack(alignment: .leading, spacing: Spacing.s) {
                Text("Find what's broken").font(.heroTitle)
                Text("Walk around it with your phone. Lea shows you what looks wrong, and where.")
                    .font(.body).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Capability and pairing live in Settings; Home shows a line under Start only when a scan can't be reported.
        }
    }

    /// Sectors drawn so far: the dome fills in once per launch, sector by sector (about a second), then stays still.
    @State private var sectorsShown = Self.played ? 12 : 0
    @MainActor private static var played = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private func drawIn() async {
        guard !Self.played else { return }
        Self.played = true
        if reduceMotion { sectorsShown = 12; return }
        // Around the dome, all three rings of a sector together, 80 ms apart; never loops.
        for step in 1...12 {
            try? await Task.sleep(for: .milliseconds(80))
            sectorsShown = step
        }
    }

    /// The header illustration: a dome part-way covered, the next cell in accent.
    static let art: [CellState] = (0..<36).map { i in
        let ring = i / 12, sector = i % 12
        if ring == 0 && sector == 5 || ring == 1 && sector == 4 { return .next }
        let covered: Set<Int> = [0, 1, 2, 3, 4, 10, 11]
        return covered.contains(sector) && ring < 2 || ring == 2 && sector != 6 && sector != 7 ? .covered : .missing
    }
}

private struct HowItWorks: View {
    private let steps: [(String, String, String)] = [
        (Symbol.lock, "Lock on", "Tap the object once."),
        ("figure.walk", "Walk around it", "The dome fills in as photos take themselves."),
        (Symbol.closeup, "A few close-ups", "Sharp shots of the parts that matter."),
        ("doc.text.magnifyingglass", "Read the report", "Most likely problems first, with photos."),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader("How it works")
            VStack(alignment: .leading, spacing: Spacing.l) {
                ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                    HStack(alignment: .top, spacing: Spacing.m) {
                        IconBadge(symbol: step.0, tone: .accent, filled: false)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.1).font(.subheadline.weight(.semibold))
                            Text(step.2).font(.footnote).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Step \(i + 1): \(step.1). \(step.2)")
                }
            }
            .card()
        }
    }
}

// MARK: - Camera permission

/// The one camera-permission screen: why, then the system prompt; if it was denied before, a way to Settings.
struct CameraPermissionView: View {
    let done: (Bool) -> Void
    @Environment(\.openURL) private var openURL
    private var denied: Bool { AVCaptureDevice.authorizationStatus(for: .video) == .denied }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            Spacer()
            ZStack {
                Circle().fill(Palette.accent.opacity(0.12))
                Image(systemName: "camera.viewfinder").font(.system(size: 34, weight: .semibold)).foregroundStyle(Palette.accent)
            }
            .frame(width: 76, height: 76)
            .accessibilityHidden(true)
            Text(denied ? "Camera is off for lea" : "lea needs the camera").font(.screenTitle)
            Text(denied ? "Turn on Camera for lea in Settings to scan."
                        : "It scans the object with the camera (and LiDAR when the phone has it) and takes close-ups for the report.")
                .font(.body).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button {
                if denied {
                    if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                    done(false)
                } else {
                    Task { done(await AVCaptureDevice.requestAccess(for: .video)) }
                }
            } label: {
                Text(denied ? "Open Settings" : "Allow camera")
            }
            .buttonStyle(.primary)
            Button("Not now") { done(false) }
                .font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .padding(.horizontal, Spacing.gutter + 4)
        .padding(.bottom, Spacing.s)
        .background(Palette.canvas.ignoresSafeArea())
        .presentationDetents([.large])
    }
}
