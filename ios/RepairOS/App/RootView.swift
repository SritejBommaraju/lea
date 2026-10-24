import AVFoundation
import Combine
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
    @StateObject private var voice = VoiceCoordinator()
    @State private var showVoiceMode = false
    @State private var scanOffered = false
    @State private var scanAfterVoiceMode = false
    @State private var offeredSymptom: String?
    @State private var handsFreeBeforeVoiceMode = false
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
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: Spacing.s) {
                    VoiceBar(voice: voice, expand: { openVoiceMode(greet: false) }).padding(.horizontal, Spacing.m)
                    startBar
                }
            }
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
                SessionFlowView(record: record).environmentObject(voice)
            }
            .fullScreenCover(isPresented: $showVoiceMode, onDismiss: afterVoiceMode) {
                VoiceModeView(voice: voice, scanOffered: scanOffered, startScan: { scanFromVoice() }, close: { showVoiceMode = false })
            }
            .alert("Lea", isPresented: Binding(get: { voice.message != nil && active == nil && !showVoiceMode }, set: { if !$0 { voice.message = nil } })) {
                Button("OK") { voice.message = nil }
            } message: { Text(voice.message ?? "") }
            .alert("Can't start", isPresented: .constant(error != nil)) {
                Button("OK") { error = nil }
            } message: { Text(error ?? "") }
            .task {
                try? await Task.sleep(for: .milliseconds(300))
                if app.autostart, active == nil { newScan() }
            }
            .onReceive(NotificationCenter.default.publisher(for: VoiceLaunch.requested)) { _ in
                if VoiceLaunch.take() { openVoiceMode(greet: true) }
            }
            .onAppear {
                connectVoice()
                if VoiceLaunch.take() { openVoiceMode(greet: true) }   // Siri opened the app before this screen existed
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
            FieldWell(symbol: "text.bubble") {
                TextField("What's wrong? (optional)", text: $symptom)
                    .focused($symptomFocused)
                    .submitLabel(.go)
                    .onSubmit(newScan)
            }
            Button(action: newScan) { Label("Start scan", systemImage: Symbol.scan) }
                .buttonStyle(.primary)
                .sensoryFeedback(Haptic.commit, trigger: starts)
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

    /// Voice talks to the gateway the app is paired with now (read at each turn), about the most recent session.
    private func connectVoice() {
        voice.attach()
        voice.wakeDetector = voice.wakeDetector ?? SoundWakeDetector()
        let app = app
        voice.link = VoiceLink(
            token: {
                guard let gateway = await app.gateway else { throw GatewayError.message("Not paired.") }
                return try await gateway.voiceToken()
            },
            turn: { request in
                let (gateway, latest) = await MainActor.run { (app.gateway, app.sessions.first?.gatewaySessionID) }
                guard let gateway else { throw GatewayError.message("Pair with the gateway in Settings to talk to Lea.") }
                var request = request
                if request.sessionID == nil && request.live.isEmpty { request.sessionID = latest }   // home: the latest scan
                return try await gateway.voiceTurn(request)
            })
        voice.onActions = { actions in
            // A scan or report already open keeps the screen; Lea talks about it instead.
            for action in actions where active == nil {
                switch action.type {
                case "offer_scan":
                    scanOffered = true
                    offeredSymptom = action.symptom
                case "scan": scanFromVoice(symptom: action.symptom)
                case "report":
                    showVoiceMode = false
                    active = app.sessions.first { $0.scanID == action.scanID } ?? app.sessions.first
                default: break
                }
            }
        }
    }

    /// Voice mode is a hands-free conversation. Siri's "Hey lea" starts a new one and Lea asks what's up; the voice
    /// bar's button picks up the current one. Closing it puts hands-free back as it was.
    private func openVoiceMode(greet: Bool) {
        handsFreeBeforeVoiceMode = voice.handsFree
        showVoiceMode = true
        if greet {
            voice.newConversation()
            voice.greetAndListen()
        } else if !voice.handsFree {
            voice.setHandsFree(true)
        }
    }

    /// A scan Lea started (or the person accepted) opens once voice mode has closed; two full-screen covers can't
    /// show at once.
    private func scanFromVoice(symptom said: String? = nil) {
        if let said = said ?? offeredSymptom, !said.isEmpty { symptom = said }
        scanOffered = false
        offeredSymptom = nil
        if showVoiceMode {
            scanAfterVoiceMode = true
            showVoiceMode = false
        } else {
            newScan()
        }
    }

    /// A scan started from voice mode keeps the conversation hands-free, so Lea talks you through it.
    private func afterVoiceMode() {
        scanOffered = false
        if !scanAfterVoiceMode && !handsFreeBeforeVoiceMode && voice.handsFree { voice.setHandsFree(false) }
        if scanAfterVoiceMode {
            scanAfterVoiceMode = false
            newScan()
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
            active = try app.storage.newSession(symptom: symptom, engine: app.capabilities.engine)
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
            DomeDial(cells: HomeHeader.art, size: 132, style: .app)
                .frame(maxWidth: .infinity)
                .padding(.vertical, Spacing.s)
            VStack(alignment: .leading, spacing: Spacing.s) {
                Text("Find what's broken").font(.heroTitle)
                Text("Point at it, walk around it, and lea writes up what looks wrong, with photos of each problem.")
                    .font(.body).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            FlowLayout(spacing: 6) {
                StatusPill(text: engine == .objectCapture ? "LiDAR scan" : "No LiDAR: motion-tracked",
                           symbol: engine == .objectCapture ? "cube.transparent" : "move.3d", tone: .neutral)
                Button(action: pair) {
                    StatusPill(text: paired ? "Mac paired" : "Mac not paired", symbol: paired ? Symbol.mac : "bolt.horizontal.circle",
                               tone: paired ? .good : .caution)
                }
                .buttonStyle(PressableCardStyle())
                .accessibilityHint(paired ? "Pair again" : "Pair with your Mac to get reports")
            }
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
