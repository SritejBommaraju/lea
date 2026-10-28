import Combine
import AVFoundation
import SwiftUI

/// Home: what lea does, the optional symptom line and one Start button within thumb reach (launch → scanning in
/// two taps: Start, then lock on), plus past scans. First run asks for consent, then offers pairing once; pairing
/// can be skipped to scan offline.
struct RootView: View {
    @EnvironmentObject private var app: AppState
    @State private var symptom = ""
    @State private var active: SessionRecord?
    @State private var showSettings = false
    @State private var showPairing = false
    @State private var showConsent = false
    @State private var scanAfterConsent = false
    @State private var failure: Failure?
    @State private var showProductCode = false
    /// A scan's files, packed and ready for the share sheet.
    @State private var shared: SharedFile?
    /// A scan is being packed for sharing; one at a time, since each pack replaces the last one's file.
    @State private var packing = false
    @State private var askCamera = false
    @State private var starts = 0
    @State private var pendingDelete: SessionRecord?
    /// Selecting several scans to delete at once (Select in the Scans header, or long-press → Select).
    @State private var selection = ScanSelection()
    @State private var confirmBatch = false
    /// The Talk button opened the sheet: it starts listening (a tap toggle; the mic stays off after the answer).
    @State private var talkListens = false
    @State private var replay: SessionRecord?
    /// "Hey Lea" (or the Talk button) on the home screen: Voice/HomeConversationSheet.
    @State private var talking = false
    @State private var talksSeen = 0
    @AppStorage(DemoReplay.enabledKey) private var demoReplay = false
    @AppStorage(DemoReplay.sessionKey) private var demoSession = ""
    @FocusState private var symptomFocused: Bool
    @Environment(\.dynamicTypeSize) private var typeSize

    /// The `--autostart-scan` developer flag skips the consent screen so USB smoke tests run unattended.
    private var devAutostart: Bool { CommandLine.arguments.contains("--autostart-scan") }

    var body: some View {
        NavigationStack {
            List {
                HomeHeader(firstRun: app.sessions.isEmpty, product: app.product?.product, settings: { showSettings = true })
                    .plainRow(top: Spacing.s)
                if app.sessions.isEmpty {
                    HowItWorks().plainRow(top: Spacing.xl)
                    HomeArt().plainRow(top: Spacing.xl, bottom: Spacing.xl)
                } else {
                    // Every scan, newest first (a lazy list): no "See all" screen.
                    VStack(spacing: Spacing.m) {
                        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                            SectionHeader("Scans", count: app.sessions.count)
                            selectControls
                        }
                        Rectangle().fill(Palette.hairline).frame(height: Line.hairline)
                    }
                    .plainRow(top: Spacing.xl, bottom: 0)
                    ForEach(app.sessions) { s in
                        ScanRowButton(record: s, open: { active = $0 }, delete: { pendingDelete = s },
                                      selecting: selection.active, selected: selection.contains(s.id),
                                      toggle: { withAnimation(Motion.fade) { selection.toggle(s.id, selectable: selectable) } },
                                      select: { withAnimation(Motion.standard) { selection.begin(with: s.id, selectable: selectable) } },
                                      share: { share(s) })
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .background(Palette.canvas.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) { if selection.active { selectBar } else { startBar } }
            .toolbar(.hidden, for: .navigationBar)
            .confirmDeleteScan($pendingDelete)
            .confirmationDialog(ScanSelection.confirmTitle(selection.count), isPresented: $confirmBatch, titleVisibility: .visible) {
                Button(ScanSelection.deleteTitle(selection.count), role: .destructive, action: deleteSelected)
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(ScanSelection.confirmMessage)
            }
            // A scan gone or being captured can't stay ticked.
            .onValueChange(of: app.sessions.map(\.id)) { selection.prune(to: selectable) }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(isPresented: $showPairing) { PairingView() }
            .sheet(isPresented: $showProductCode) { ProductCodeView() }
            .sheet(item: $shared, onDismiss: ScanExport.clear) { file in ShareSheet(items: [file.url], finished: { shared = nil }) }
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
            .alert(failure?.title ?? "Can't start", isPresented: Binding(get: { failure != nil }, set: { if !$0 { failure = nil } })) {
                // Where deleting old scans happens: lea's own Settings.
                if failure?.action == .freeSpace { Button("Open lea Settings") { failure = nil; showSettings = true } }
                Button("OK", role: .cancel) { failure = nil }
            } message: { Text(failure?.message ?? "") }
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
            .onReceive(IntentInbox.shared.$requests.receive(on: DispatchQueue.main)) { _ in scanFromIntent() }
            // "Hey Siri, hey lea" (Voice/ScanIntent): the conversation opens listening, and Lea asks what's up.
            .onReceive(IntentInbox.shared.$talks.receive(on: DispatchQueue.main)) { talks in
                guard talks > talksSeen else { return }
                talksSeen = talks
                talkToLea()
            }
            .onReceive(WakeListener.shared.$wakes.dropFirst().receive(on: DispatchQueue.main)) { _ in if active == nil, replay == nil, app.gateway != nil { talkListens = false; talking = true } }
            .sheet(isPresented: $talking) {
                if let gateway = app.gateway {
                    HomeConversationSheet(gateway: gateway, listenOnOpen: talkListens) { said in
                        symptom = said
                        newScan()
                    }
                }
            }
            .onAppear {
                #if DEBUG
                // Design review only: open in selection mode with the first deletable scan ticked.
                if CommandLine.arguments.contains("--ui-select") {
                    selection.begin(with: app.sessions.first { ScanLibrary.canDelete($0, app: app) }?.id, selectable: selectable)
                }
                #endif
                if !Consent.accepted && !devAutostart {
                    showConsent = true
                } else {
                    offerPairingOnce()
                }
            }
        }
        .tint(Palette.accent)
    }

    /// The talk bar (DESIGN §7.1): the optional symptom, then Talk to Lea (a quiet capsule) beside Start scan (blue,
    /// the wider). The one frosted bar on the screen. Not paired: Start scan alone, and a way to set up.
    private var startBar: some View {
        GlassBar {
            FieldWell(symbol: "text.bubble") {
                TextField(app.product == nil ? "Describe it (optional)" : "What's wrong? (optional)", text: $symptom)
                    .focused($symptomFocused)
                    .submitLabel(.go)
                    .onSubmit(newScan)
                StoryMicButton(text: $symptom, gateway: app.isPaired ? app.gateway : nil)   // Voice/: tap to dictate
            }
            // Side by side at normal text sizes; one above the other when the words would be cut off.
            let stacked = typeSize >= .xxxLarge
            let layout = stacked ? AnyLayout(VStackLayout(spacing: Spacing.s)) : AnyLayout(HStackLayout(spacing: Spacing.s))
            layout {
                if app.isPaired, app.gateway != nil {
                    Button(action: talkToLea) { TalkLabel() }
                        .buttonStyle(.primary(.quiet))
                        .widthFromScreen { width in stacked ? width - 2 * Spacing.gutter : (width - 2 * Spacing.gutter - Spacing.s) * 0.425 }
                        .accessibilityLabel("Talk to Lea")
                        .accessibilityHint("Opens Lea, already listening")
                }
                Button(action: newScan) { Label("Start scan", systemImage: Symbol.scan) }
                    .buttonStyle(.primary)
                    .haptic(Haptic.commit, trigger: starts)
            }
            if !app.isPaired {
                // The only plumbing on Home, and only when it blocks a report.
                Button { showProductCode = true } label: {
                    Text(app.product == nil ? "Not connected. Add your product code" : "Not connected. Finish setting up")
                        .font(.footnote.weight(.semibold)).foregroundStyle(Palette.caution)
                        .multilineTextAlignment(.center)
                }
                .frame(minHeight: 44)
                .accessibilityHint("Opens setup")
            }
            if demoReplay, !demoSession.isEmpty, let saved = DemoReplay.chosen(app: app) {
                // Demo only: a quiet text button, never as heavy as Start scan.
                Button { replay = saved } label: { Label("Replay saved scan", systemImage: "play") }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Palette.accent)
                    .frame(minHeight: 44)
            }
        }
        // The bar stays usable at the largest text sizes: past this size it would fill the screen and hide the scans.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
        .contentShape(Rectangle())
        // Swipe up stays as an accelerator for the labelled Talk to Lea.
        .gesture(DragGesture(minimumDistance: 20).onEnded { if $0.translation.height < -30 { talkToLea() } })
        .accessibilityAction(named: "Talk to Lea") { talkToLea() }
    }

    /// Talk to Lea (or swipe up on the bar): Lea's conversation opens already listening (the talk button's recording:
    /// tap Lea's mark to send). After she answers the mic stays off until the next tap (the owner, 2026-10-25).
    private func talkToLea() {
        guard app.isPaired, app.gateway != nil, active == nil, !selection.active else { return }
        talkListens = true
        talking = true
    }

    // MARK: Selecting scans

    /// The scans that can be ticked (never the one being captured).
    private var selectable: Set<String> { Set(app.sessions.filter { ScanLibrary.canDelete($0, app: app) }.map(\.id)) }

    /// "Select" in the Scans header; in selection mode "Select all" / "Deselect all" and "Done".
    @ViewBuilder private var selectControls: some View {
        if selection.active {
            Button(selection.allSelected(selectable) ? "Deselect all" : "Select all") {
                withAnimation(Motion.fade) { selection.toggleAll(selectable) }
            }
            .disabled(selectable.isEmpty)
            Button("Done") { withAnimation(Motion.standard) { selection.end() } }
                .fontWeight(.semibold)
        } else {
            Button("Select") { withAnimation(Motion.standard) { selection.begin(selectable: selectable) } }
                .disabled(selectable.isEmpty)
        }
    }

    /// Selection mode's bar, in place of the talk bar: Delete N scans (danger) and Cancel.
    private var selectBar: some View {
        GlassBar {
            HStack(spacing: Spacing.s) {
                Button("Cancel") { withAnimation(Motion.standard) { selection.end() } }
                    .buttonStyle(.primary(.quiet))
                Button { confirmBatch = true } label: {
                    Label(ScanSelection.deleteTitle(selection.count), systemImage: "trash")
                }
                .buttonStyle(.primary(.danger))
                .disabled(selection.count == 0)
            }
            Text(selection.count == 0 ? "Tap scans to select them." : "\(selection.count) selected")
                .font(.footnote).foregroundStyle(Palette.secondary)
                .contentTransition(.numericText())
        }
    }

    /// One batched delete, after the confirmation has gone (so the List applies one update, not one mid-dismissal),
    /// with selection mode ending in the same update: the list and the bar change together, never racing.
    private func deleteSelected() {
        let doomed = app.sessions.filter { selection.contains($0.id) && ScanLibrary.canDelete($0, app: app) }
        guard !doomed.isEmpty else { return }
        Task { @MainActor in
            withAnimation {
                ScanLibrary.delete(doomed, app: app)
                selection.end()
            }
        }
    }

    /// Packs the scan's folder as one zip, then opens the share sheet.
    private func share(_ record: SessionRecord) {
        guard !packing else { return }
        packing = true
        let folder = app.storage.dir(record)
        Task {
            defer { packing = false }
            if let url = await ScanExport.zip(folder, name: "lea-scan-\(record.id)") {
                shared = SharedFile(url: url)
            } else {
                failure = Failure(title: "Couldn't pack the scan files",
                                  message: "The phone may be out of space. Free some up, then try again.", action: .freeSpace)
            }
        }
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

    /// First run: ask for the product code once. The code carries the server, so nobody types an address; the
    /// address form is still one tap away on that screen.
    private func offerPairingOnce() {
        if !app.isPaired && !UserDefaults.standard.bool(forKey: "pairingOffered") {
            UserDefaults.standard.set(true, forKey: "pairingOffered")
            showProductCode = true
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
            failure = .storageLow(freeBytes: app.freeBytes, needed: AppState.minimumFreeBytes)
            return
        }
        do {
            // After talking with Lea first (Voice/PreScan): the same gateway session and scan id carry on.
            active = try PreScan.shared.takeRecord(storage: app.storage, symptom: symptom, engine: app.capabilities.engine)
                ?? app.storage.newSession(symptom: symptom, engine: app.capabilities.engine)
            symptom = ""
        } catch {
            self.failure = Failure.from(error)
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

/// The masthead (the `lea` wordmark with her blue point, Settings as a round outline button), and on first run the
/// title and the line from `main`.
private struct HomeHeader: View {
    let firstRun: Bool
    /// The product the person added a code for; the header names it instead of asking what the object is.
    var product: String?
    let settings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            HStack(alignment: .center) {
                HStack(alignment: .lastTextBaseline, spacing: 3) {
                    Text("lea").display(.wordmark).tracking(-0.34).foregroundStyle(Palette.ink)
                    Circle().fill(Palette.accent).frame(width: 7, height: 7).padding(.bottom, 6)
                }
                .accessibilityElement()
                .accessibilityLabel("lea")
                .accessibilityAddTraits(.isHeader)
                Spacer()
                Button(action: settings) { Image(systemName: Symbol.settings) }
                    .buttonStyle(RoundIconButtonStyle())
                    .accessibilityLabel("Settings")
            }
            if firstRun {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(product == nil ? "Find what's broken" : "Check your \(product ?? "product")").display(.hero).foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Walk around it with your phone. lea shows what looks wrong, and where.")
                        .font(.body).foregroundStyle(Palette.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // Capability and pairing live in Settings; Home shows a line under Start only when a scan can't be reported.
        }
    }
}

/// The first-run art: the coverage dome as an astrolabe (the product's own mark, not a placeholder illustration),
/// drawing in once per launch, sector by sector (about a second), then still. Reduce Motion: drawn at once.
private struct HomeArt: View {
    @State private var sectorsShown = Self.played ? 12 : 0
    @MainActor private static var played = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        DomeDial(cells: Self.art.enumerated().map { $0.offset % 12 < sectorsShown ? $0.element : .missing }, size: 132, style: .app)
            .frame(maxWidth: .infinity)
            .accessibilityHidden(true)
            .task { await drawIn() }
    }

    private func drawIn() async {
        guard !Self.played else { return }
        Self.played = true
        if reduceMotion { sectorsShown = 12; return }
        // Around the dome, all three rings of a sector together, 80 ms apart; never loops. Leaving cancels it.
        for step in 1...12 {
            do { try await Task.sleep(for: .milliseconds(80)) } catch { sectorsShown = 12; return }
            sectorsShown = step
        }
    }

    /// The dome part-way covered, the next cell in accent.
    static let art: [CellState] = (0..<36).map { i in
        let ring = i / 12, sector = i % 12
        if ring == 0 && sector == 5 || ring == 1 && sector == 4 { return .next }
        let covered: Set<Int> = [0, 1, 2, 3, 4, 10, 11]
        return covered.contains(sector) && ring < 2 || ring == 2 && sector != 6 && sector != 7 ? .covered : .missing
    }
}

/// Talk to Lea's label: her mark, or the ear while "Hey Lea" listens (the old separate indicator, moved inside).
private struct TalkLabel: View {
    @ObservedObject private var wake = WakeListener.shared

    var body: some View {
        HStack(spacing: Spacing.s) {
            if wake.mode == .off {
                LeaMarkView(size: 24).accessibilityHidden(true)
            } else {
                Image(systemName: wake.mode == .followUp ? "ear.badge.waveform" : "ear")
                    .font(.body).foregroundStyle(Palette.accent)
                    .frame(width: 24, height: 24)
                    .accessibilityLabel(wake.mode == .followUp ? "Lea is listening" : "Listening for Hey Lea")
            }
            Text("Talk to Lea")
        }
    }
}

/// How it works: the four steps from `main` as a numbered index (the numbers are a real sequence).
private struct HowItWorks: View {
    private let steps: [(String, String)] = [
        ("Point at it", "Aim the phone at the product and tap it once."),
        ("Walk around it", "Go slowly. The ring fills in as the phone takes photos."),
        ("Take a few close-ups", "The phone asks for sharp shots of the parts it wants to check."),
        ("Read the report", "The most likely problem comes first, with photos. Add a meter reading if it asks."),
    ]
    @ScaledMetric(relativeTo: .subheadline) private var numberColumn: CGFloat = 20

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader("How it works")
            Rectangle().fill(Palette.hairline).frame(height: Line.hairline)
            VStack(alignment: .leading, spacing: Spacing.l) {
                ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                        Text("\(i + 1)").display(.number).foregroundStyle(Palette.ink)
                            .frame(width: numberColumn, alignment: .leading)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.0).font(.subheadline.weight(.semibold)).foregroundStyle(Palette.ink)
                            Text(step.1).font(.footnote).foregroundStyle(Palette.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Step \(i + 1): \(step.0). \(step.1)")
                }
            }
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
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                Image(systemName: "camera.viewfinder").font(.largeTitle).foregroundStyle(Palette.secondary)
                    .accessibilityHidden(true)
                Text(denied ? "Camera is off for lea" : "lea needs the camera").display(.screen).foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                Text(denied ? "lea scans with the camera. Turn Camera on for lea in Settings, then come back."
                            : "lea looks at your product through the camera, with LiDAR if the phone has it, and takes close-ups for the report.")
                    .font(.body).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, Spacing.gutter + 4)
            .padding(.top, Spacing.xxl * 2)
            .padding(.bottom, Spacing.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: Spacing.s) {
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
                    .buttonStyle(.textAction)
                    .frame(maxWidth: .infinity)
            }
            .padding(.horizontal, Spacing.gutter + 4)
            .padding(.bottom, Spacing.s)
            .background(Palette.canvas)
        }
        .background(Palette.canvas.ignoresSafeArea())
        .presentationDetents([.large])
    }
}

/// A file handed to the share sheet.
struct SharedFile: Identifiable {
    let id = UUID()
    let url: URL
}
