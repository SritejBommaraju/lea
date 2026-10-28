import Foundation
import os

let log = Logger(subsystem: "com.repairos.app", category: "app")

/// App-wide state: what the device can do, where the gateway is, and the local sessions.
@MainActor
final class AppState: ObservableObject {
    @Published private(set) var capabilities = DeviceCapabilities.current()
    @Published private(set) var stillProbe: StillProbeResult? = StillProbeResult.cached()
    @Published private(set) var probing = false
    @Published private(set) var sessions: [SessionRecord] = []

    @Published var gatewayURL: String {
        didSet { UserDefaults.standard.set(gatewayURL, forKey: "gatewayURL") }
    }

    /// This phone holds a device token for the server (set by `pair`, cleared by `unpair`).
    @Published private(set) var isPaired = Keychain.token() != nil
    /// The product code the person scanned or typed (which server, which product), if any.
    @Published private(set) var product: ProductCode? = ProductCode.stored()

    let storage = ScanStorage()
    let launchedAt = Date()
    /// Set by the `--autostart-scan` developer flag: open a new scan straight away (smoke tests over USB).
    @Published private(set) var autostart = false
    /// Set by `--open-session <id>` (developer flag): the saved scan to open at launch.
    @Published var openSessionID: String?

    /// Free space for the scan images and reconstruction; Object Capture needs room for ~100+ HEICs plus a checkpoint.
    static let minimumFreeBytes: Int64 = 1 << 30

    var freeBytes: Int64 {
        let v = try? storage.root.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return v?.volumeAvailableCapacityForImportantUsage ?? 0
    }

    init() {
        gatewayURL = UserDefaults.standard.string(forKey: "gatewayURL") ?? ""
    }

    func launch() async {
        reloadSessions()
        // Developer hooks for `devicectl device process launch <bundle> ...` from the Mac over USB:
        // `--gateway <url> --pairing-code <code>` pairs without typing; `--run-still-probe` measures stills.
        let args = CommandLine.arguments
        func value(_ flag: String) -> String? {
            args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
        }
        if let url = value("--gateway") { gatewayURL = url }
        if let code = value("--pairing-code") {
            do { try await pair(code: code) } catch { log.error("pairing failed: \(error.localizedDescription, privacy: .public)") }
        }
        if args.contains("--run-still-probe") { await runStillProbe() }
        autostart = args.contains("--autostart-scan")
        // `--open-session <id>`: open that saved scan at launch (its report); `--show-on-object` then opens the AR view.
        openSessionID = value("--open-session")
        // `--demo-replay-session <id>`: turn Demo replay on for that saved scan (and show the Settings section).
        if let id = value("--demo-replay-session") {
            UserDefaults.standard.set(true, forKey: DemoReplay.enabledKey)
            UserDefaults.standard.set(true, forKey: DemoReplay.unlockedKey)
            UserDefaults.standard.set(id, forKey: DemoReplay.sessionKey)
        }
        capabilities = DeviceCapabilities.current()
        writeDiagnostics()
    }

    /// Documents/diagnostics.json: what the probes found, readable from a Mac with `devicectl device copy from`.
    func writeDiagnostics() {
        let c = capabilities
        var out: [String: Any] = ["model": c.model, "ios": c.ios, "world_tracking": c.worldTracking,
                                  "scene_depth": c.sceneDepth, "mesh": c.meshReconstruction,
                                  "rear_cameras": c.rearCameras, "video_formats": c.videoFormats,
                                  "engine": c.engine.rawValue]
        if let p = stillProbe, let data = try? JSON.encoder.encode(p) {
            out["still_probe"] = try? JSONSerialization.jsonObject(with: data)
        }
        if let data = try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: storage.root.deletingLastPathComponent().appending(path: "diagnostics.json"))
        }
    }

    /// The still probe never blocks launch: the first close-up measures it inside the capture session (or
    /// Settings runs it on demand), and the answer is cached per iOS build.
    func reloadSessions() {
        // Unique ids only: a duplicate id in a List's ForEach also breaks its row bookkeeping.
        var seen = Set<String>()
        sessions = storage.listSessions().filter { seen.insert($0.id).inserted }
        stillProbe = StillProbeResult.cached()
    }

    func runStillProbe() async {
        guard !probing else { return }
        probing = true
        defer { probing = false }
        stillProbe = await StillProbe.run()
        stillProbe?.store()
        writeDiagnostics()
    }

    var gateway: GatewayClient? {
        var text = gatewayURL.trimmingCharacters(in: .whitespaces)
        if !text.isEmpty, !text.contains("://") { text = "http://" + text }
        guard let url = URL(string: text), url.scheme?.hasPrefix("http") == true, url.host() != nil,
              URLComponents(url: url, resolvingAgainstBaseURL: false) != nil else {
            return nil
        }
        return GatewayClient(baseURL: url, token: Keychain.token())
    }

    func pair(code: String) async throws {
        guard let gateway else { throw GatewayError.message("There is no server address yet. Add your product code, or type the address.") }
        let token = try await gateway.pair(code: code, name: capabilities.model)
        try Keychain.setToken(token)
        isPaired = true
        Task { await LeaVoice.shared.refresh() }
    }

    func unpair() {
        Keychain.deleteToken()
        isPaired = false
    }

    /// Remembers a product code: its server becomes the gateway (a phone paired to a different server is unpaired, since
    /// its token belongs to the old one) and its product id goes to the server with every new scan.
    func use(_ code: ProductCode) throws {
        try Keychain.setProduct(code)
        product = code
        if Self.sameServer(gatewayURL, code.gateway) { return }
        gatewayURL = code.gateway
        if isPaired { unpair() }
    }

    #if DEBUG
    /// The screens harness changed the Keychain, settings and saved scans under the running app: read them again.
    func screensReload() {
        isPaired = Keychain.token() != nil
        product = ProductCode.stored()
        gatewayURL = UserDefaults.standard.string(forKey: "gatewayURL") ?? ""
        reloadSessions()
    }
    #endif

    /// Forgets the product code (the server and the pairing stay).
    func clearProduct() {
        Keychain.deleteProduct()
        product = nil
    }

    nonisolated static func sameServer(_ a: String, _ b: String) -> Bool {
        func norm(_ s: String) -> String {
            var t = s.trimmingCharacters(in: .whitespaces).lowercased()
            if !t.isEmpty, !t.contains("://") { t = "http://" + t }
            while t.hasSuffix("/") { t.removeLast() }
            return t
        }
        return norm(a) == norm(b)
    }
}
