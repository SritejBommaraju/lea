import Combine
import QuickLook
import RealityKit
import SwiftUI

/// What the app can be asked to do from anywhere (a button or, through the agent, a spoken request).
@MainActor
final class LeaActions: ObservableObject {
    struct Closeup: Equatable { let subject: String; let box: AgentAction.Box? }
    struct Whiteboard: Identifiable { let id = UUID(); let title: String; let steps: [AgentAction.Step]; var backdrop: Data? = nil }

    @Published var tab = 0
    @Published var scanning = false
    @Published var closeup: Closeup?
    @Published var whiteboard: Whiteboard?
    /// A candidate the person tapped on the 3D model; sent with the next spoken turn.
    @Published var focusFindingId: String?
    /// The live camera view as JPEG, while Capture is on screen; sent with a spoken turn so Lea can see it.
    var frameProvider: (() async -> Data?)?

    /// Does what the agent asked the phone to do.
    func perform(_ actions: [AgentAction]) {
        for action in actions {
            switch action.type {
            case "scan": scanning = true
            case "screen": tab = ["capture": 1, "repair": 3, "design": 4][action.screen ?? ""] ?? tab
            case "closeup" where !scanning:   // the guided scan owns the camera; a close-up waits until it closes
                tab = 1; closeup = Closeup(subject: action.subject ?? "", box: action.box)
            case "whiteboard":
                let board = Whiteboard(title: action.title ?? "Whiteboard", steps: action.steps ?? [])
                if action.over == "camera" {
                    Task { var b = board; b.backdrop = await frameProvider?(); whiteboard = b }   // drawn over what the camera sees
                } else {
                    whiteboard = board
                }
            default: break
            }
        }
    }
}

/// Guided 3D scan on iPhones with LiDAR. Lea speaks Apple's capture feedback; the stills are kept as evidence
/// whatever happens, and only an explicit Discard deletes them.
struct ScanView: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var conversation: VoiceCoordinator
    @EnvironmentObject private var actions: LeaActions
    @StateObject private var scanner = ObjectScanner()
    @State private var preview: URL?
    @State private var confirmClose = false
    @State private var saving = false

    var body: some View {
        ZStack {
            if let session = scanner.session {
                ObjectCaptureView(session: session).ignoresSafeArea()
            } else {
                Color.black.ignoresSafeArea()
            }
            VStack {
                HStack {
                    Button { confirmClose = true } label: { Image(systemName: "xmark.circle.fill").font(.title) }
                    Spacer()
                    if scanner.shots > 0 { Text("\(scanner.shots) photos").font(.caption.weight(.semibold)).padding(8).background(.ultraThinMaterial, in: Capsule()) }
                }
                if let tip = scanner.feedback { Text(tip).font(.headline).padding(10).background(.ultraThinMaterial, in: Capsule()) }
                Spacer()
                controls.padding().background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
            }
            .padding()
        }
        .onAppear { scanner.start() }
        .onChange(of: scanner.feedback) { if let tip = scanner.feedback { conversation.sayAside(tip) } }
        .quickLookPreview($preview)
        .confirmationDialog("Stop scanning?", isPresented: $confirmClose) {
            if scanner.shots > 0 { Button("Save the photos taken so far") { Task { await saveStillsOnly() } } }
            Button("Discard everything", role: .destructive) { scanner.cancel(); actions.scanning = false }
            Button("Keep scanning", role: .cancel) {}
        }
        .interactiveDismissDisabled()
    }

    @ViewBuilder private var controls: some View {
        switch scanner.phase {
        case .idle, .preparing:
            step("Point at the object on a plain surface.", "Find object") { scanner.advance() }
        case .detecting:
            step("Adjust the box around the object.", "Start scanning") { scanner.advance() }
        case .capturing:
            Text("Walk slowly all the way around, at a few heights.").font(.subheadline)
        case .passDone:
            VStack(spacing: 10) {
                Text("Pass done. Another pass from a different height, flip it for the underside, or finish.").font(.subheadline)
                HStack {
                    Button("Another pass") { scanner.advance() }
                    Button("Flip") { scanner.flip() }
                    Button("Finish") { scanner.finish() }.buttonStyle(.borderedProminent).tint(.mint)
                }
                .buttonStyle(.bordered)
            }
        case .finishing:
            ProgressView("Finishing capture…")
        case .reconstructing(let fraction):
            ProgressView("Building the 3D model on this iPhone…", value: fraction)
        case .done:
            VStack(spacing: 10) {
                Text("Model ready. It still has to pass the measurement checks before Lea uses it for diagnosis or CAD.").font(.subheadline)
                HStack {
                    if let usdz = scanner.result?.usdz { Button("Preview") { preview = usdz }.buttonStyle(.bordered) }
                    Button(saving ? "Saving…" : "Save to session") { Task { await save() } }.buttonStyle(.borderedProminent).tint(.mint).disabled(saving)
                }
            }
        case .failed(let message):
            VStack(spacing: 10) {
                Text(message).font(.subheadline)
                if scanner.result != nil { Button("Save the photos") { Task { await save() } }.buttonStyle(.borderedProminent).tint(.mint) }
            }
        }
    }

    private func step(_ text: String, _ title: String, action: @escaping () -> Void) -> some View {
        VStack(spacing: 10) {
            Text(text).font(.subheadline)
            Button(title, action: action).buttonStyle(.borderedProminent).tint(.mint)
        }
    }

    private func save() async {
        guard let result = scanner.takeResult() else { return }
        saving = true
        await store.addScan(images: result.images, usdz: result.usdz, obj: result.obj,
                            engine: result.obj == nil ? "apple-object-capture-ios (photos only)" : "apple-object-capture-ios/reduced")
        saving = false
        actions.scanning = false
        await sync.syncActiveSession()
    }

    private func saveStillsOnly() async {
        guard let images = scanner.stopKeepingPhotos() else { return }
        await store.addScan(images: images, usdz: nil, obj: nil, engine: "apple-object-capture-ios (photos only, stopped early)")
        actions.scanning = false
        await sync.syncActiveSession()
    }
}
