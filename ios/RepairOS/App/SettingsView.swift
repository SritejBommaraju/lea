import SwiftUI

struct PairingView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var code = ""
    @State private var busy = false
    @State private var error: String?
    @State private var paired = 0

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.xl) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text("Connect to the lea server").display(.screen).foregroundStyle(Palette.ink)
                            .accessibilityAddTraits(.isHeader)
                        Text("lea sends your scan to the lea server, which writes the report. Enter its address and pairing code.")
                            .font(.body).foregroundStyle(Palette.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: Spacing.l) {
                        LabeledWell("Server address") {
                            TextField("192.168.1.10:8000", text: $app.gatewayURL)
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        }
                        LabeledWell("Pairing code") {
                            SecureField("Required", text: $code)   // masked as you type
                        }
                        Text("For developers: on the server, run `make run-gateway`. The code is REPAIROS_PAIRING_CODE in backend/.env.")
                            .font(.footnote).foregroundStyle(Palette.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error {
                        Label(error, systemImage: "exclamationmark.circle")
                            .font(.callout).foregroundStyle(Palette.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, Spacing.gutter)
                .padding(.top, Spacing.s)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(Palette.canvas.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) {
                GlassBar {
                    Button { Task { await pair() } } label: {
                        if busy { ProgressView().tint(Palette.onAccent) } else { Text("Pair") }
                    }
                    .buttonStyle(.primary)
                    .disabled(busy || code.isEmpty || app.gatewayURL.isEmpty)
                    .haptic(.success, trigger: paired)
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Skip") { dismiss() } }
            }
        }
        .tint(Palette.accent)
    }

    private func pair() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await app.pair(code: code)
            paired += 1
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var app: AppState
    @ObservedObject private var voice = LeaVoice.shared
    @Environment(\.dismiss) private var dismiss
    @State private var showPairing = false
    @State private var showConsent = false
    @State private var health: Result<Void, Error>?
    @State private var checking = false
    @State private var used: Int64?
    @State private var confirmDeleteAll = false
    @State private var versionTaps = 0
    @AppStorage(DemoReplay.unlockedKey) private var demoUnlocked = false
    @AppStorage(DesignID.key) private var designID = ""

    var body: some View {
        NavigationStack {
            Form {
                Group {
                    Section {
                        HStack(spacing: Spacing.m) {
                            Image(systemName: Symbol.mac).foregroundStyle(app.isPaired ? Palette.good : Palette.caution)
                                .frame(width: 28).accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(app.isPaired ? "Connected" : "Not connected").font(.body.weight(.semibold)).foregroundStyle(Palette.ink)
                                Text(app.gatewayURL.isEmpty ? "No address set" : app.gatewayURL)
                                    .font(.footnote).foregroundStyle(Palette.secondary).lineLimit(1).truncationMode(.middle)
                            }
                            Spacer(minLength: 0)
                            if let health { healthPill(health) }
                        }
                        Button(app.isPaired ? "Connect again" : "Connect to lea server") { showPairing = true }
                        Button { Task { await check() } } label: {
                            HStack {
                                Text("Check connection")
                                Spacer()
                                if checking { ProgressView() }
                            }
                        }
                        .disabled(checking || app.gateway == nil)
                    } header: { Text("Server") } footer: {
                        Text("Scans stay on the phone until it is paired.")
                    }

                    Section {
                        TextField("Design id on the product", text: $designID)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                    } header: { Text("Product") } footer: {
                        Text("The design id printed on the product. Lea judges each scan against that design.")
                    }

                    Section {
                        Toggle("Listen for \u{201C}Hey Lea\u{201D}", isOn: Binding(get: { WakeListener.shared.enabled },
                                                                        set: { WakeListener.shared.enabled = $0 }))
                        Toggle("Lea speaks", isOn: Binding(get: { !voice.muted }, set: { voice.muted = !$0 }))
                    } header: { Text("Voice") } footer: {
                        Text("\u{201C}Hey Lea\u{201D} is heard on this phone only, while lea is open. Nothing is sent until you say it.")
                    }

                    Section {
                        NavigationLink { UsageView() } label: {
                            SettingsRow(symbol: "gauge.with.dots.needle.33percent", tone: .accent, title: "Usage",
                                        detail: "Calls and estimated cost today, on the server")
                        }
                        .disabled(app.gateway == nil || !app.isPaired)
                    }

                    Section("Privacy") {
                        Button { showConsent = true } label: {
                            SettingsRow(symbol: "photo.badge.checkmark", tone: .accent, title: "Where photos go",
                                        detail: "The lea server, then Claude and Jev")
                        }
                        .foregroundStyle(Palette.ink)
                    }

                    Section {
                        LabeledContent("Scans", value: "\(app.sessions.count)")
                        LabeledContent("Storage used", value: used.map(ScanLibrary.size) ?? "…")
                        Button("Delete all scans", role: .destructive) { confirmDeleteAll = true }
                            .disabled(ScanLibrary.deletableCount(app: app) == 0)
                    } header: { Text("Storage") } footer: {
                        Text("Deleting removes only the copy on this phone; the server keeps its copy.")
                    }

                    Section("This phone") {
                        let c = app.capabilities
                        LabeledContent("Scan method", value: "Motion-tracked")
                        LabeledContent("LiDAR depth", value: c.sceneDepth ? "Yes" : "No")
                        NavigationLink("Diagnostics") { DiagnosticsView() }
                    }

                    if demoUnlocked { DemoReplaySettings() }

                    Section {
                        NavigationLink { NoticesView() } label: {
                            SettingsRow(symbol: "doc.plaintext", tone: .neutral, title: "Open source licences")
                        }
                    } footer: {
                        Text("lea \(Self.version)").frame(maxWidth: .infinity).padding(.top, Spacing.l)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                // Five taps on the version show the hidden Demo section.
                                versionTaps += 1
                                if versionTaps >= 5 { withAnimation(Motion.standard) { demoUnlocked = true } }
                            }
                    }
                }
                .listRowBackground(Palette.surface)
            }
            .settingsList()
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { Text("Settings").display(.card).foregroundStyle(Palette.ink) }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) }
            }
            .sheet(isPresented: $showPairing) { PairingView() }
            .sheet(isPresented: $showConsent) { ConsentView() }
            .confirmationDialog(deleteAllTitle, isPresented: $confirmDeleteAll, titleVisibility: .visible) {
                Button("Delete \(ScanLibrary.deletableCount(app: app)) scans", role: .destructive) {
                    // After the dialog has gone, like a single delete: one list update, not one mid-dismissal.
                    Task { @MainActor in
                        withAnimation { ScanLibrary.deleteAll(app: app) }
                        used = await ScanLibrary.usedBytes(app.storage)
                    }
                }
            } message: {
                Text("Removes every scan, photo and report from this phone\(used.map { " and frees \(ScanLibrary.size($0))" } ?? ""). Copies on the server stay.")
            }
            .task { used = await ScanLibrary.usedBytes(app.storage) }
        }
        .tint(Palette.accent)
    }

    private var deleteAllTitle: String {
        "Delete all \(ScanLibrary.deletableCount(app: app)) scans\(used.map { " (\(ScanLibrary.size($0)))" } ?? "")?"
    }

    @ViewBuilder private func healthPill(_ r: Result<Void, Error>) -> some View {
        switch r {
        case .success: StatusPill(text: "Online", symbol: "checkmark", tone: .good)
        case .failure: StatusPill(text: "Can't reach", symbol: "xmark", tone: .danger)
        }
    }

    private func check() async {
        checking = true
        defer { checking = false }
        do {
            try await app.gateway?.health()
            withAnimation(Motion.standard) { health = .success(()) }
        } catch {
            withAnimation(Motion.standard) { health = .failure(error) }
        }
    }

    static let version: String = {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] as? String ?? "?") (\(info?["CFBundleVersion"] as? String ?? "?"))"
    }()
}

/// A Settings row: a bare outline symbol (no tinted square), the title and an optional detail.
private struct SettingsRow: View {
    let symbol: String
    let tone: Tone
    let title: String
    var detail: String?

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: symbol).foregroundStyle(tone == .neutral ? Palette.secondary : tone.color)
                .frame(width: 28).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).foregroundStyle(Palette.ink)
                if let detail { Text(detail).font(.footnote).foregroundStyle(Palette.secondary) }
            }
        }
    }
}

/// A field with its label above it, in a `well` (Pairing).
private struct LabeledWell<Field: View>: View {
    let label: String
    @ViewBuilder var field: Field
    init(_ label: String, @ViewBuilder field: () -> Field) { self.label = label; self.field = field() }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text(label).font(.label).foregroundStyle(Palette.secondary)
            field.font(.body).foregroundStyle(Palette.ink)
                .padding(.horizontal, Spacing.l)
                .frame(minHeight: 52)
                .background(Palette.well, in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
        }
    }
}

extension View {
    /// Settings' native inset-grouped list, recoloured: canvas behind (rows set `surface` themselves), 44 pt rows.
    func settingsList() -> some View {
        scrollContentBackground(.hidden)
            .background(Palette.canvas.ignoresSafeArea())
            .environment(\.defaultMinListRowHeight, 44)
    }
}

/// Developer details: video formats and the close-up resolution probe.
struct DiagnosticsView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        Form {
            Group {
                Section("Device") {
                    LabeledContent("Model", value: app.capabilities.model)
                    LabeledContent("iOS", value: app.capabilities.ios)
                }
                Section("Cameras") {
                    let c = app.capabilities
                    LabeledContent("Scene mesh", value: c.meshReconstruction ? "Yes" : "No")
                    LabeledContent("Rear cameras", value: c.rearCameras.joined(separator: ", "))
                    DisclosureGroup("Video formats") {
                        ForEach(c.videoFormats, id: \.self) { Text($0).font(.caption.monospaced()) }
                    }
                }
                Section("Close-up resolution") {
                    if let p = app.stillProbe {
                        LabeledContent("Decision", value: p.resolution.rawValue)
                        LabeledContent("Max dimensions", value: p.useMaxDimensions ? "used" : "not used")
                        ForEach(Array(p.attempts.enumerated()), id: \.offset) { _, a in
                            Text("\(a.format) req \(a.requested ?? "default") → \(a.width)×\(a.height) in \(a.latencyMs) ms\(a.hasSceneDepth ? ", depth" : "")\(a.error.map { " ✕ \($0)" } ?? "")")
                                .font(.caption.monospaced())
                        }
                    } else {
                        Text("Not measured yet: the first close-up measures it.").font(.callout).foregroundStyle(Palette.secondary)
                    }
                    Button(app.probing ? "Measuring…" : "Measure now") { Task { await app.runStillProbe() } }
                        .disabled(app.probing)
                }
            }
            .listRowBackground(Palette.surface)
        }
        .settingsList()
        .navigationTitle("Diagnostics")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct NoticesView: View {
    var body: some View {
        ScrollView {
            Text(Self.text).font(.caption.monospaced()).textSelection(.enabled)
                .padding(Spacing.gutter)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Palette.canvas.ignoresSafeArea())
        .navigationTitle("Licences")
        .navigationBarTitleDisplayMode(.inline)
    }

    static let text: String = Bundle.main.url(forResource: "THIRD_PARTY_NOTICES", withExtension: "md")
        .flatMap { try? String(contentsOf: $0, encoding: .utf8) } ?? "Notices file missing from the app bundle."
}
