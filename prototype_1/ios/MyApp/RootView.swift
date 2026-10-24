import QuickLook
import SwiftUI
import UniformTypeIdentifiers

struct RootView: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var conversation: VoiceCoordinator
    @EnvironmentObject private var actions: LeaActions
    @StateObject private var voice = VoiceRecorder()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView(selection: $actions.tab) {
            SessionView(selectedTab: $actions.tab)
                .tabItem { Label("Session", systemImage: "square.stack.3d.up") }.tag(0)
            CaptureView()
                .tabItem { Label("Capture", systemImage: "camera.viewfinder") }.tag(1)
            EvidenceView()
                .tabItem { Label("Evidence", systemImage: "square.grid.2x2") }.tag(2)
            RepairView()
                .tabItem { Label("Repair", systemImage: "wrench.and.screwdriver") }.tag(3)
            DesignView()
                .tabItem { Label("Design", systemImage: "cube") }.tag(4)
        }
        .tint(.mint)
        .safeAreaInset(edge: .bottom) {
            VoiceBar(voice: voice)
                .environmentObject(store)
                .padding(.horizontal, 14)
                .padding(.bottom, 4)
        }
        .onAppear { voice.onFinished = { url, duration in store.addVoiceClip(from: url, duration: duration) } }
        .onChange(of: store.activeSessionID) { Task { await sync.syncActiveSession() } }
        .onChange(of: scenePhase) { if scenePhase == .active { Task { await sync.syncActiveSession() } } }   // resume uploads and saved requests
        .fullScreenCover(isPresented: $actions.scanning) { ScanView() }
        .sheet(item: $actions.whiteboard) { WhiteboardView(lesson: $0) }
        .alert("Lea", isPresented: Binding(
            get: { store.errorMessage != nil || voice.message != nil || sync.message != nil || conversation.message != nil },
            set: { if !$0 { clearMessages() } }
        )) {
            Button("OK") { clearMessages() }
        } message: {
            Text(store.errorMessage ?? voice.message ?? sync.message ?? conversation.message ?? "")
        }
    }

    private func clearMessages() {
        store.errorMessage = nil
        voice.message = nil
        sync.message = nil
        conversation.message = nil
    }
}

private struct SessionView: View {
    @EnvironmentObject private var store: CaptureStore
    @Binding var selectedTab: Int
    @State private var showingSettings = false
    @State private var deleting: CaptureSession?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("REPAIR WORKSPACE").font(.caption.weight(.bold)).tracking(2).foregroundStyle(.mint)
                        Text("Capture the object.\nKeep the evidence.")
                            .font(.largeTitle.bold())
                        Text("Build a photo set that can be reviewed before reconstruction and CAD handoff.")
                            .foregroundStyle(.secondary)
                    }

                    VStack(alignment: .leading, spacing: 12) {
                        Label("Active session", systemImage: "circle.dotted.circle.fill")
                            .font(.headline)
                        Text(store.activeSession.createdAt, format: .dateTime.month(.abbreviated).day().hour().minute())
                            .font(.title3.weight(.semibold))
                        HStack(spacing: 18) {
                            countLabel(store.activeSession.photos.count, "photos")
                            countLabel(store.activeSession.voiceClips.count, "voice notes")
                            countLabel(store.activeSession.meshReferences.count, "meshes")
                        }
                        Button { selectedTab = 1 } label: {
                            Label("Continue capture", systemImage: "camera.fill")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.mint)
                    }
                    .padding(18)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))

                    ScanPipelineCard()

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Sessions").font(.headline)
                        ForEach(store.sessions) { session in
                            Button { store.selectSession(session.id) } label: {
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text(session.createdAt, format: .dateTime.month().day().hour().minute())
                                        Text("\(session.photos.count) photos · \(session.meshReferences.count) meshes")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if session.id == store.activeSessionID { Image(systemName: "checkmark.circle.fill").foregroundStyle(.mint) }
                                }
                            }
                            .buttonStyle(.plain)
                            .contextMenu { Button("Delete from this iPhone", role: .destructive) { deleting = session } }
                            Divider()
                        }
                        Button { store.newSession() } label: { Label("New session", systemImage: "plus.circle.fill") }
                            .padding(.top, 4)
                    }
                    .padding(18)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
                }
                .padding()
            }
            .navigationTitle("Lea")
            .toolbar {
                Button { showingSettings = true } label: { Image(systemName: "gearshape") }
            }
            .sheet(isPresented: $showingSettings) { BridgeSettingsView() }
            .confirmationDialog("Delete this session's photos, voice notes and scans from this iPhone?", isPresented: Binding(
                get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                Button("Delete", role: .destructive) { if let s = deleting { store.deleteSession(s.id) }; deleting = nil }
            } message: { Text("Anything already uploaded stays on the bridge.") }
        }
    }

    private func countLabel(_ count: Int, _ title: String) -> some View {
        VStack(alignment: .leading) {
            Text("\(count)").font(.title2.bold())
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct CaptureView: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var conversation: VoiceCoordinator
    @EnvironmentObject private var actions: LeaActions
    @StateObject private var camera = CameraService()
    @StateObject private var coach = CaptureCoach()
    @State private var role: PhotoRole = .orbit
    @State private var subject = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Walk around the object slowly. Keep each view overlapping, then capture close-ups of labels, connectors, fasteners and damage.")
                        .font(.subheadline).foregroundStyle(.secondary)

                    ZStack {
                        CameraPreview(camera: camera)
                        CoachOverlay(seen: coach.seen, frameSize: coach.frameSize)
                        if !camera.isReady {
                            Color.black.opacity(0.7)
                            ContentUnavailableView("Camera unavailable", systemImage: "camera.fill", description: Text(camera.message ?? "Requesting camera access…"))
                        }
                    }
                    .frame(height: 390)
                    .clipShape(RoundedRectangle(cornerRadius: 22))

                    if ObjectScanner.supported {
                        Button { if store.lowOnSpace { store.errorMessage = "This iPhone is nearly out of space. A scan needs a few hundred megabytes." } else { actions.scanning = true } } label: {
                            Label("3D scan (guided)", systemImage: "cube.transparent").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent).tint(.mint)
                    } else {
                        Text("This iPhone has no LiDAR, so take Orbit photos below; the Mac builds the 3D model from them.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Toggle("Coach", isOn: $coach.on).toggleStyle(.button).tint(.mint)
                        Toggle("Speak tips", isOn: $coach.speakTips).toggleStyle(.button).disabled(!coach.on)
                        Spacer()
                    }
                    if !coach.status.isEmpty {
                        Label(coach.status, systemImage: "eye").font(.subheadline)
                            .foregroundStyle((coach.seen?.looksDamaged ?? 0) >= 0.6 ? .orange : .primary)
                    }
                    if coach.on, role == .closeup {
                        Text(coach.referenceAssetId != nil ? "Comparing with your reference photo of \(coach.referenceSubject ?? subject)."
                             : coach.referenceSubject != nil ? "Uploading the reference photo…"
                             : "No reference photo of this area, so only photo quality is checked.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Picker("Photo type", selection: $role) {
                        ForEach(PhotoRole.allCases) { Text($0.title).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    if role != .orbit {
                        TextField(role == .observation ? "What this result shows" : "Area shown (e.g. fan connector)", text: $subject)
                            .textFieldStyle(.roundedBorder)
                    }

                    HStack {
                        Label("\(store.activeSession.photos.filter { $0.effectiveRole == role }.count) \(role.title.lowercased())", systemImage: "photo.stack")
                        Spacer()
                        Button { if store.lowOnSpace { store.errorMessage = "This iPhone is nearly out of space. Free some space before taking more photos." } else { camera.takePhoto() } } label: {
                            Image(systemName: "camera.fill")
                                .font(.title2).frame(width: 68, height: 68)
                        }
                        .buttonStyle(.borderedProminent).tint(.mint)
                        .clipShape(Circle())
                        .disabled(!camera.isReady)
                    }
                    .padding(.horizontal, 8)

                    VStack(alignment: .leading, spacing: 9) {
                        Text("Capture guide").font(.headline)
                        guideRow("1", "Full orbit", "Cover every visible side with overlapping views.")
                        guideRow("2", "Top and underside", "Move the object safely if needed.")
                        guideRow("3", "Close-up evidence", "Capture wiring, PCB areas and damage separately.")
                        guideRow("4", "Measure two sides", "Calipers on two different sides: one sets scale, the other tests it.")
                        guideRow("5", "Reference views", "Photograph the same areas on a known-good unit as Reference.")
                    }
                    .padding(18)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
                }
                .padding()
            }
            .navigationTitle("Capture")
            .onAppear {
                camera.onPhoto = { data in store.addPhoto(data, role: role, subject: subject.isEmpty ? nil : subject) }
                camera.start()
                coach.camera = camera
                coach.sync = sync
                coach.voice = conversation
                updateCoachTarget()
                actions.frameProvider = { await camera.liveFrame()?.jpeg }   // cleared in onDisappear
            }
            .onChange(of: role) {
                camera.onPhoto = { data in store.addPhoto(data, role: role, subject: subject.isEmpty ? nil : subject) }
                updateCoachTarget()
            }
            .onChange(of: subject) {
                camera.onPhoto = { data in store.addPhoto(data, role: role, subject: subject.isEmpty ? nil : subject) }
                updateCoachTarget()
            }
            .onChange(of: store.activeSession.photos.count) { updateCoachTarget() }
            .onChange(of: store.activeSession.uploadedAssetIds?.count) { updateCoachTarget() }
            .onDisappear { coach.on = false; camera.stop(); actions.frameProvider = nil }
            .onChange(of: actions.closeup) { if let request = actions.closeup { Task { await takeCloseup(request) } } }
            // The guided scan needs the camera to itself, however it was opened (button or voice).
            .onChange(of: actions.scanning) {
                if actions.scanning { coach.on = false; camera.stop() } else { camera.start() }
            }
            .alert("Camera", isPresented: Binding(
                get: { camera.message != nil },
                set: { if !$0 { camera.message = nil } }
            )) {
                Button("OK") { camera.message = nil }
            } message: { Text(camera.message ?? "") }
        }
    }

    /// "Zoom in on it for me": zoom toward the part, take a full-resolution close-up, then compare it with the saved
    /// reference photo of the same area when there is one.
    private func takeCloseup(_ request: LeaActions.Closeup) async {
        defer { actions.closeup = nil }
        role = .closeup
        subject = request.subject
        camera.onPhoto = { data in store.addPhoto(data, role: .closeup, subject: request.subject.isEmpty ? nil : request.subject) }
        camera.zoom(to: request.box)
        try? await Task.sleep(for: .seconds(1))            // let focus and exposure settle
        let before = store.activeSession.photos.count
        camera.takePhoto()
        for _ in 0..<50 where store.activeSession.photos.count == before { try? await Task.sleep(for: .milliseconds(100)) }
        camera.zoom(to: nil)
        guard store.activeSession.photos.count > before, let shot = store.activeSession.photos.last else {
            conversation.sayAside("I couldn't take that photo. Try again.")
            return
        }
        let reference = store.activeSession.photos.last {
            $0.effectiveRole == .reference && !request.subject.isEmpty && $0.subject?.localizedCaseInsensitiveContains(request.subject) == true
        }
        guard let reference else {
            conversation.sayAside("Got the close-up. There's no reference photo of the \(request.subject.isEmpty ? "part" : request.subject) yet, so I can't compare it.")
            return
        }
        guard sync.health?.vision.anthropicConfigured == true else {
            conversation.sayAside("Got the close-up. Comparing it needs the assistant's key on the laptop.")
            return
        }
        await sync.syncActiveSession()
        if await sync.perform({ client, remote in
            try await client.compareCloseup(sessionId: remote, closeupAssetId: SessionSync.assetId(shot.id), referencePhotoAssetId: SessionSync.assetId(reference.id),
                                            subject: request.subject, requestId: "cmp-\(shot.id.uuidString)")
        }) != nil {
            conversation.sayAside("Comparing it with the reference now. The result will show up in Repair.")
        }
    }

    /// Coach against the saved reference photo of the same area, once that photo is on the bridge.
    private func updateCoachTarget() {
        coach.subject = subject
        let match = role == .closeup && !subject.isEmpty
            ? store.activeSession.photos.last { $0.effectiveRole == .reference && $0.subject?.localizedCaseInsensitiveContains(subject) == true }
            : nil
        coach.referenceSubject = match?.subject
        guard let match else { coach.referenceAssetId = nil; return }
        if (store.activeSession.uploadedAssetIds ?? []).contains(match.id.uuidString) {
            coach.referenceAssetId = SessionSync.assetId(match.id)
        } else {
            // Not on the bridge yet: upload it; the uploaded-count change below re-runs this and picks it up.
            coach.referenceAssetId = nil
            Task { await sync.syncActiveSession() }
        }
    }

    private func guideRow(_ number: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(number).font(.caption.bold()).foregroundStyle(.black)
                .frame(width: 24, height: 24).background(.mint, in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct EvidenceView: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @State private var importingMesh = false
    @State private var meshUnitsPerMm = 1.0
    @State private var scanPreview: URL?
    private let columns = [GridItem(.flexible()), GridItem(.flexible())]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Original captures remain on this device with a SHA-256 digest. Imported meshes are references until scale and fit are validated.")
                        .font(.subheadline).foregroundStyle(.secondary)
                    Picker("Reference units", selection: $meshUnitsPerMm) {
                        Text("Millimetres").tag(1.0)
                        Text("Metres").tag(0.001)
                        Text("Inches").tag(1 / 25.4)
                    }
                    .pickerStyle(.segmented)
                    Button { importingMesh = true } label: {
                        Label("Import reference mesh", systemImage: "square.and.arrow.down")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent).tint(.mint)
                    Button { Task { await sync.syncActiveSession() } } label: {
                        Label(sync.isSyncing ? "Uploading originals…" : "Upload originals to the bridge", systemImage: "arrow.up.circle")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .disabled(sync.isSyncing || sync.client == nil)

                    if !store.activeSession.meshReferences.isEmpty {
                        Text("Reference meshes").font(.headline)
                        ForEach(store.activeSession.meshReferences) { mesh in
                            Label(mesh.filename, systemImage: "cube.transparent")
                                .font(.subheadline).lineLimit(1)
                        }
                    }
                    if let scans = store.activeSession.scans, !scans.isEmpty {
                        Text("3D scans").font(.headline)
                        ForEach(scans) { scan in
                            Button {
                                if let usdz = scan.usdzFilename { scanPreview = store.sessionURL(for: store.activeSession).appendingPathComponent(usdz) }
                            } label: {
                                Label("\(scan.createdAt.formatted(date: .abbreviated, time: .shortened)) · \(scan.photoIds.count) photos\(scan.usdzFilename == nil ? " · no model yet" : "")",
                                      systemImage: "cube.transparent").font(.subheadline)
                            }
                            .disabled(scan.usdzFilename == nil)
                        }
                    }
                    if !store.activeSession.voiceClips.isEmpty {
                        Text("Voice notes").font(.headline)
                        ForEach(store.activeSession.voiceClips) { clip in
                            Label("\(clip.recordedAt.formatted(date: .abbreviated, time: .shortened)) · \(Int(clip.duration)) s", systemImage: "waveform")
                                .font(.subheadline)
                        }
                    }
                    Text("Photos").font(.headline)
                    if store.activeSession.photos.isEmpty {
                        ContentUnavailableView("No photos yet", systemImage: "photo.on.rectangle", description: Text("Open Capture to start a photo set."))
                    } else {
                        LazyVGrid(columns: columns, spacing: 12) {
                            ForEach(store.activeSession.photos.reversed()) { photo in
                                VStack(alignment: .leading, spacing: 4) {
                                    if let image = UIImage(contentsOfFile: store.photoURL(photo, in: store.activeSession).path) {
                                        Image(uiImage: image).resizable().scaledToFill()
                                            .frame(height: 145).frame(maxWidth: .infinity).clipped()
                                            .clipShape(RoundedRectangle(cornerRadius: 12))
                                    }
                                    Text("\(photo.effectiveRole.title) · \(photo.capturedAt.formatted(date: .omitted, time: .standard))")
                                        .font(.caption).foregroundStyle(.secondary)
                                    if let subject = photo.subject { Text(subject).font(.caption2).lineLimit(1) }
                                    Text("\(photo.width) × \(photo.height)")
                                        .font(.caption2).foregroundStyle(.tertiary)
                                }
                            }
                        }
                    }
                }
                .padding()
            }
            .navigationTitle("Evidence")
            .quickLookPreview($scanPreview)
            .fileImporter(isPresented: $importingMesh, allowedContentTypes: [.data]) { result in
                switch result {
                case .success(let url): store.importMesh(from: url, unitsPerMm: meshUnitsPerMm)
                case .failure(let error): store.errorMessage = "File import failed: \(error.localizedDescription)"
                }
            }
        }
    }
}

private struct VoiceBar: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var conversation: VoiceCoordinator
    @ObservedObject var voice: VoiceRecorder

    var body: some View {
        VStack(spacing: 8) {
            ConversationBar()
            noteBar
        }
    }

    private var noteBar: some View {
        HStack(spacing: 12) {
            Image(systemName: voice.isRecording ? "waveform" : "mic.fill")
                .foregroundStyle(voice.isRecording ? .red : .mint)
            VStack(alignment: .leading, spacing: 2) {
                Text(voice.isRecording ? "Recording voice note" : "Voice note")
                    .font(.subheadline.weight(.semibold))
                Text("Saved locally with its hash")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            Button(voice.isRecording ? "Stop" : "Record") { voice.toggle() }
                .buttonStyle(.borderedProminent)
                .tint(voice.isRecording ? .red : .mint)
                .disabled(conversation.state != .idle)
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

/// Push-to-talk (hold) and foreground "Hey Leah"; both go through the one VoiceCoordinator.
private struct ConversationBar: View {
    @EnvironmentObject private var conversation: VoiceCoordinator
    @State private var pressing = false

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(conversation.partial.isEmpty ? (conversation.lastReply ?? subtitle) : conversation.partial)
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
            Toggle(isOn: Binding(get: { conversation.handsFree }, set: { conversation.setHandsFree($0) })) {
                Text("Hands-free").font(.caption2)
            }
            .toggleStyle(.button)
            if conversation.wakeAvailable {
                Toggle(isOn: Binding(get: { conversation.wakeOn }, set: { conversation.setWake($0) })) { Text("Hey Leah").font(.caption2) }
                    .toggleStyle(.button)
            }
            Image(systemName: pressing ? "mic.circle.fill" : "mic.circle")
                .font(.system(size: 38))
                .foregroundStyle(pressing ? .red : .mint)
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { _ in if !pressing { pressing = true; conversation.pushToTalkBegan() } }
                    .onEnded { _ in pressing = false; conversation.pushToTalkEnded() })
                .accessibilityLabel("Hold to talk to Lea")
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private var title: String {
        switch conversation.state {
        case .idle: "Talk to Lea"
        case .listening: conversation.handsFree ? "Listening (hands-free)" : "Listening"
        case .thinking: "Thinking"
        case .speaking: "Lea is speaking"
        }
    }
    private var subtitle: String {
        "\(conversation.handsFree ? "Just talk; you can interrupt" : "Hold the mic to talk") · \(conversation.listenerName)\(conversation.wakeOn ? " · say “Hey Leah”" : "")"
    }
}
