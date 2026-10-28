#if DEBUG
import SwiftUI

/// DEBUG only: puts the app into one fixed state and shows one screen, so the UI tests (`ios/RepairOSUITests`) can
/// screenshot every screen on a simulator, and so a person can open the same screen by hand:
///
///     -screens consent | home-empty | home-scans | settings | pairing | report | meter | capture | capture-locked
///              | camera-permission | progress-error
///
/// Each launch wipes the app's saved scans, token and settings first, then seeds only what the screen needs. The one
/// report used is the contract example in `spec/contracts/examples` (a test fixture, never shipped: this file is not
/// in Release builds). Nothing here talks to a server.
enum ScreensHarness {
    static var screen: String? { UIGallery.value("-screens") }

    /// Resets the app to a known state. Called once, before the app's state object reads anything.
    @MainActor
    static func prepare() {
        guard let name = screen else { return }
        try? FileManager.default.removeItem(at: ScanStorage().root)
        let storage = ScanStorage()   // recreates the empty folder
        Keychain.deleteToken()
        let d = UserDefaults.standard
        for key in ["gatewayURL", DesignID.key, DemoReplay.enabledKey, DemoReplay.unlockedKey, DemoReplay.sessionKey] {
            d.removeObject(forKey: key)
        }
        d.set(true, forKey: "pairingOffered")
        if name == "consent" { d.removeObject(forKey: Consent.key) } else { d.set(Consent.version, forKey: Consent.key) }

        switch name {
        case "home-scans":
            // Paired, so the screen shows the Talk bar as a customer sees it.
            d.set("http://127.0.0.1:9", forKey: "gatewayURL")
            try? Keychain.setToken("screens-fixture-token")
            seedSessions(storage)
        case "report":
            seedSessions(storage)
        default:
            break
        }
    }

    @MainActor @ViewBuilder
    static func root(_ name: String, app: AppState) -> some View {
        switch name {
        case "consent": ConsentView { _ in }
        case "home-empty", "home-scans": RootView()
        case "settings": SettingsView()
        case "pairing": PairingView()
        case "report":
            if let record = app.sessions.first {
                ReportView(record: record, upload: nil, close: {}, offline: true)
            } else {
                Palette.canvas.ignoresSafeArea()
            }
        case "meter":
            Palette.canvas.ignoresSafeArea().sheet(isPresented: .constant(true)) {
                MeterReadingSheet(context: MeterReadingContext(kind: .continuity, net: "VBUS", pins: ["J1.A4", "F1.1"],
                                                               safetyNotes: ["Switch the product off and unplug it first."]),
                                  scanID: "scan_example", sessionID: "sess_example", gateway: nil, sent: {})
            }
        case "capture": CaptureOverlayPreview(backdrop: nil)
        case "capture-locked": CaptureOverlayPreview(backdrop: nil, locked: true)
        case "camera-permission": CameraPermissionView { _ in }
        case "progress-error":
            NavigationStack {
                ReportProgressView(progress: PipelineProgress(step: .inspect, stillsDone: 5, stillsTotal: 13), uploaded: 40,
                                   pending: 0, error: "Can't reach the lea server", retry: {})
                    .background(Palette.canvas.ignoresSafeArea())
            }
        default: UIGallery.root(name, app: app)
        }
    }

    // MARK: - Fixtures

    /// Three scans: one with a report, one that stopped, one still uploading. Fixed dates, so screenshots match run to run.
    @MainActor
    private static func seedSessions(_ storage: ScanStorage) {
        let engine = DeviceCapabilities.current().engine
        let base = Date(timeIntervalSince1970: 1_793_000_000)
        func make(_ symptom: String?, _ status: SessionRecord.Status, hoursAgo: Double, stills: [String] = [], error: String? = nil) -> SessionRecord? {
            guard var r = try? storage.newSession(symptom: symptom, engine: engine) else { return nil }
            r.createdAt = base.addingTimeInterval(-hoursAgo * 3600)
            r.status = status
            r.stillIDs = stills
            r.lastError = error
            try? storage.save(r)
            return r
        }
        if let reported = make("Lamp flickers when I touch the base", .reported, hoursAgo: 2, stills: ["s1"]),
           let json = (try? JSONSerialization.jsonObject(with: Data(reportFixture.utf8))) as? [String: Any] {
            CachedReport.save(json, record: reported, storage: storage)
        }
        _ = make(nil, .failed, hoursAgo: 26, stills: ["s1", "s2", "s3"], error: "Can't reach the lea server")
        _ = make("Won't charge", .uploading, hoursAgo: 50, stills: ["s1", "s2"])
    }

    /// The contract example report (spec/contracts/examples/report.json), trimmed to what the screen reads.
    private static let reportFixture = #"""
    {"report_id":"rep_scan_example","scan_id":"scan_example","session_id":"sess_example","created_at":"2026-10-24T12:00:00Z",
     "verdict":"potential_issues",
     "verdict_reason":{"code":"issues_found","rule":"V5","text":"At least one inspected region shows a potential issue."},
     "summary":{"text":"One connector may be loose. It is a suspicion from the photos, not proof. Take a closer photo of it as the next step.","written_by":"test fixture","from_words_only":true},
     "findings":[{"finding_id":"f_example_a","subject":{"kind":"part","ref":"s1/det0"},"part_class":"connector","issue":"loose",
       "confidence_word":"medium","evidence":["connector in still s1 looks loose","a gap is visible between the plug and its housing"],
       "inspected_by":["grain"],"crop":{"still_id":"s1","box_px":[100,200,612,712]},
       "location":{"status":"not_computed","reason":"no pose"},"proof":{"status":"not_measured"},
       "judgment":{"rank":1,"likelihood_word":"unsure","verdict":"suspect","explains_symptom":"yes"},"severity":"functional"}],
     "most_likely":["f_example_a"],"cosmetic":[],
     "not_inspected":[{"finding_id":"f_example_c","subject":{"kind":"part","ref":"s1/det2"},"part_class":"circuit board",
       "photo":{"still_id":"s1"},"reason":"the region covers most of the still; take a closer still"}],
     "insufficient_evidence":[{"finding_id":"f_example_d","subject":{"kind":"wire","ref":"s1/det3"},"part_class":"wire",
       "photo":{"still_id":"s1"},"reason":"could not judge the wire in still s1: it is blurred"}],
     "next_step":{"action_id":"act_example","kind":"request_closeup",
       "instruction":"Take a close, sharp photo of the part s1/det0. Fill the frame with it and hold the phone steady.",
       "reason":"The evidence about the part is not clear enough to judge it."},
     "capture":{"depth_source":"lidar","stills":1,"stills_inspected":1,"keyframes":2,"scan_points":1000,"coverage_words":"partly covered"}}
    """#
}
#endif
