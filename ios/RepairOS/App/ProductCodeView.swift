@preconcurrency import AVFoundation
import SwiftUI

/// First run, and Settings > Product: add the code that came with the product, so lea knows which server and which
/// product it is looking at (format: `ProductCode`). The camera reads a QR code; where there is no camera (the
/// simulator, a denied permission) or the code is on paper, the same screen takes it typed or pasted. When the code
/// does not carry a pairing code, the next step asks for it. Skipping leaves the old flow: set up a server by hand and
/// describe what is being scanned.
struct ProductCodeView: View {
    /// Settings > Change product code: show the scanner and the field even though a code is saved.
    var replacing = false
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @State private var typed = ""
    @State private var problem: String?
    @State private var pairingCode = ""
    @State private var busy = false
    @State private var camera = CameraAccess.current
    @State private var showServer = false
    @State private var accepted = 0
    /// Still waiting for a code (always, with none saved; with one saved, only while it is being replaced).
    @State private var entering: Bool

    init(replacing: Bool = false) {
        self.replacing = replacing
        _entering = State(initialValue: replacing || Keychain.product() == nil)
    }

    /// The code the app holds right now; set once a code was read and saved.
    private var code: ProductCode? { entering ? nil : app.product }
    private var needsPairing: Bool { code != nil && !app.isPaired }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.xl) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text(code == nil ? "Add your product" : "Connect to the server").display(.screen).foregroundStyle(Palette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityAddTraits(.isHeader)
                        Text(code == nil
                             ? "Scan the code that came with your product, or type it. Then lea knows what it is checking."
                             : "One more step. Enter the pairing code so this phone can send scans.")
                            .font(.body).foregroundStyle(Palette.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let code {
                        summary(code)
                        if needsPairing { pairingStep }
                    } else {
                        scanner
                        typedStep
                    }
                    if let problem {
                        Label(problem, systemImage: "exclamationmark.circle")
                            .font(.callout).foregroundStyle(Palette.danger)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel("Problem. \(problem)")
                    }
                }
                .padding(.horizontal, Spacing.gutter)
                .padding(.top, Spacing.s)
                .padding(.bottom, Spacing.xl)
                .frame(maxWidth: 600, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(Palette.canvas.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) { bottomBar }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(code == nil ? "Skip" : "Not now") { dismiss() } }
            }
            .sheet(isPresented: $showServer) { PairingView() }
            .haptic(.success, trigger: accepted)
            .task { await prepareCamera() }
            // Back from the phone's Settings with the camera now allowed: show the scanner without reopening this screen.
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                Task { await prepareCamera() }
            }
        }
        .tint(Palette.accent)
    }

    // MARK: Steps

    @ViewBuilder private var scanner: some View {
        switch camera {
        case .ready:
            QRScannerView { text in read(text) }
                .frame(height: 260)
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
                .accessibilityElement()
                .accessibilityLabel("Camera. Point it at the QR code that came with your product.")
        case .denied:
            FailureCard(failure: Failure.cameraDenied) { _ in openSettings() }
        case .unavailable:
            HStack(spacing: Spacing.m) {
                Image(systemName: "camera.metering.none").foregroundStyle(Palette.secondary).accessibilityHidden(true)
                Text("This device has no camera to scan with. Type the code below.")
                    .font(.subheadline).foregroundStyle(Palette.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .card()
        case .asking:
            ProgressView().frame(maxWidth: .infinity, minHeight: 120)
        }
    }

    private var typedStep: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("Or type the code").font(.label).foregroundStyle(Palette.secondary)
            FieldWell(symbol: "number") {
                TextField(ProductCode.example, text: $typed)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .submitLabel(.go)
                    .onSubmit { read(typed) }
                    .accessibilityLabel("Product code")
            }
            Button("Use another server address instead") { showServer = true }
                .buttonStyle(.textAction)
        }
    }

    private func summary(_ code: ProductCode) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Label("Product code accepted", systemImage: "checkmark.seal").font(.label).foregroundStyle(Palette.good)
            LabeledContent("Product", value: code.product)
            LabeledContent("Server", value: code.host)
        }
        .font(.body)
        .card()
        .accessibilityElement(children: .combine)
    }

    private var pairingStep: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("Pairing code").font(.label).foregroundStyle(Palette.secondary)
            FieldWell(symbol: "key") {
                SecureField("From the paperwork", text: $pairingCode)
                    .submitLabel(.go)
                    .onSubmit { Task { await connect(pairingCode) } }
                    .accessibilityLabel("Pairing code")
            }
        }
    }

    private var bottomBar: some View {
        GlassBar {
            if code == nil {
                Button { read(typed) } label: { Text("Use this code") }
                    .buttonStyle(.primary)
                    .disabled(typed.trimmingCharacters(in: .whitespaces).isEmpty)
            } else if needsPairing {
                Button { Task { await connect(pairingCode) } } label: {
                    if busy { ProgressView().tint(Palette.onAccent) } else { Text("Connect") }
                }
                .buttonStyle(.primary)
                .disabled(busy || pairingCode.isEmpty)
                Button("Remove this product code") { app.clearProduct(); problem = nil }
                    .buttonStyle(.textAction)
            } else {
                Button("Done") { dismiss() }.buttonStyle(.primary)
            }
        }
    }

    // MARK: Actions

    /// A code from the camera or the field. Ignored while one is being connected.
    private func read(_ text: String) {
        guard !busy, entering else { return }
        guard let parsed = ProductCode.parse(text) else {
            problem = "That doesn't look like a product code. \(ProductCode.help)"
            return
        }
        problem = nil
        do {
            try app.use(parsed)
        } catch {
            problem = Failure.from(error).text
            return
        }
        accepted += 1
        entering = false
        if let pairing = parsed.pairing {
            Task { await connect(pairing) }
        } else if app.isPaired {
            dismiss()
        }
    }

    private func connect(_ pairing: String) async {
        let trimmed = pairing.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !busy else { return }
        busy = true
        problem = nil
        defer { busy = false }
        do {
            try await app.pair(code: trimmed)
            dismiss()
        } catch {
            problem = Failure.from(error, context: .pairing).text
        }
    }

    private func prepareCamera() async {
        camera = CameraAccess.current
        if camera == .asking {
            camera = await CameraAccess.request() ? .ready : .denied
        }
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
    }
}

/// Whether the camera can read a QR code right now.
enum CameraAccess {
    case ready, denied, unavailable, asking

    static var current: CameraAccess {
        guard AVCaptureDevice.default(for: .video) != nil else { return .unavailable }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: return .ready
        case .notDetermined: return .asking
        default: return .denied
        }
    }

    static func request() async -> Bool { await AVCaptureDevice.requestAccess(for: .video) }
}

// MARK: - QR scanner

/// A live camera picture that reports each QR code it sees (the same code many times a second, so the receiver must
/// ignore repeats). Stops the camera when it leaves the screen.
struct QRScannerView: UIViewRepresentable {
    let onCode: @MainActor (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    func makeUIView(context: Context) -> QRPreviewView {
        let view = QRPreviewView()
        context.coordinator.attach(to: view)
        return view
    }

    func updateUIView(_ uiView: QRPreviewView, context: Context) {}

    static func dismantleUIView(_ uiView: QRPreviewView, coordinator: Coordinator) { coordinator.stop() }

    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate, @unchecked Sendable {
        private let session = AVCaptureSession()
        private let queue = DispatchQueue(label: "lea.qr")
        private let onCode: @MainActor (String) -> Void

        init(onCode: @escaping @MainActor (String) -> Void) { self.onCode = onCode }

        @MainActor func attach(to view: QRPreviewView) {
            guard let device = AVCaptureDevice.default(for: .video),
                  let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else { return }
            session.addInput(input)
            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else { return }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: .main)
            output.metadataObjectTypes = [.qr]
            view.previewLayer.session = session
            view.previewLayer.videoGravity = .resizeAspectFill
            let box = SessionBox(session: session)
            queue.async { box.session.startRunning() }
        }

        func stop() {
            let box = SessionBox(session: session)
            queue.async { if box.session.isRunning { box.session.stopRunning() } }
        }

        func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject],
                            from connection: AVCaptureConnection) {
            guard let text = (metadataObjects.first as? AVMetadataMachineReadableCodeObject)?.stringValue else { return }
            Task { @MainActor in self.onCode(text) }
        }
    }

    private struct SessionBox: @unchecked Sendable { let session: AVCaptureSession }
}

/// A view whose layer is the camera preview.
final class QRPreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
}
