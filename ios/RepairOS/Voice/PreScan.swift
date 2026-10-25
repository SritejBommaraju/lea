import Foundation
import Observation
import SwiftUI
import UIKit

/// Talking with Lea before the scan, so she can work out what the object is. The conversation gets a gateway
/// session and a scan id at once: what the person says goes to `POST /v1/sessions/{id}/story`, a "Show me the
/// label" photo goes in as a `label` still of that scan (the backend identifies the object from it and sends
/// `identified`, then `product_brief` with what public sources say), and the scan that follows uses the same
/// session and scan id, so the conversation simply carries on at its report.
@MainActor
@Observable
final class PreScan {
    static let shared = PreScan()

    enum LabelState: Equatable { case none, sending, sent, failed(String) }

    private(set) var sessionID: String?
    private(set) var scanID = PreScan.newScanID()
    private(set) var live: LiveEvents?
    private(set) var label: LabelState = .none
    @ObservationIgnored private var starting: Task<Void, Never>?

    static func newScanID() -> String { "scan_" + ScanStorage.hex(8) }

    /// The gateway session for this conversation (made once).
    func begin(gateway: GatewayClient) async {
        if sessionID != nil { return }
        if let starting { await starting.value; return }
        let t = Task { [weak self] in
            guard let self else { return }
            if let id = try? await gateway.createSession(symptom: nil) {
                sessionID = id
                let events = LiveEvents(scanID: scanID, diag: WakeListener.diag)
                events.start(gateway: gateway) { [weak self] in self?.sessionID }
                live = events
                WakeListener.diag.log("prescan_session", ["ok": true])
            } else {
                WakeListener.diag.log("prescan_session", ["ok": false])
            }
        }
        starting = t
        await t.value
        starting = nil
    }

    /// One photo of the label: uploaded as a blob, then `POST /v1/sessions/{id}/identify`.
    func sendLabel(_ image: UIImage, gateway: GatewayClient) async {
        guard let sid = sessionID else { label = .failed("Not connected to the lea server."); return }
        label = .sending
        do {
            let up = image.normalizedUp()
            let jpeg = up.jpegData(compressionQuality: 0.88) ?? Data()
            let file = URL.temporaryDirectory.appending(path: "lea-label-\(ScanStorage.hex(4)).jpg")
            try jpeg.write(to: file)
            defer { try? FileManager.default.removeItem(at: file) }
            let ref = try await gateway.uploadBlob(file: file, sha256: GatewayClient.sha256(of: file), contentType: "image/jpeg")
            // POST /v1/sessions/{id}/identify {image}: the backend reads the label and sends `product_brief`.
            var req = gateway.voiceRequest("POST", "v1/sessions/\(sid)/identify")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: ["image": ref])
            req.timeoutInterval = 20
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard (200..<300).contains(code) else { throw GatewayError.http(code, "") }
            label = .sent
            WakeListener.diag.log("prescan_label", ["ok": true, "bytes": jpeg.count])
        } catch {
            label = .failed("The photo didn't go through: \(error.localizedDescription)")
            WakeListener.diag.log("prescan_label", ["ok": false, "error": error.localizedDescription])
        }
    }

    /// The scan that starts now uses this conversation's session and scan id (nil when there was no conversation).
    func takeRecord(storage: ScanStorage, symptom: String?, engine: EngineKind) throws -> SessionRecord? {
        guard let sid = sessionID else { return nil }
        let stamp = ISO8601DateFormatter.compact.string(from: Date())
        var record = SessionRecord(id: "sess_local_\(stamp)_\(ScanStorage.hex(4))", createdAt: Date(),
                                   symptom: symptom?.trimmedOrNil, productID: "prod_\(ScanStorage.hex(8))",
                                   scanID: scanID, engine: engine)
        record.gatewaySessionID = sid
        try FileManager.default.createDirectory(at: storage.scanDir(record), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: storage.stillsDir(record), withIntermediateDirectories: true)
        try storage.save(record)
        // The upload reuses the session the conversation made instead of creating another.
        var state = UploadQueue.State()
        state.gatewaySessionID = sid
        try JSON.encoder.encode(state).write(to: storage.dir(record).appending(path: "upload.json"), options: .atomic)
        WakeListener.diag.log("prescan_scan", ["label": label == .sent])
        live?.stop()
        live = nil
        sessionID = nil
        label = .none
        scanID = Self.newScanID()
        return record
    }
}

/// `product_brief` (live event): what the object is and what public sources say about it. Read leniently: the
/// backend's own words, never invented here.
struct ProductBrief: Equatable, Sendable {
    var name: String?
    var brand: String?
    var model: String?
    var knownIssues: [String]
    var issueAreas: [String] = []
    var category: String?
    var sources: [String]

    init?(json o: [String: Any]) {
        func text(_ v: Any?) -> String? {
            if let s = v as? String { return s.trimmedOrNil }
            if let d = v as? [String: Any] { return text(d["text"] ?? d["title"] ?? d["issue"] ?? d["name"]) }
            return nil
        }
        name = text(o["name"])
        brand = text(o["brand"])
        model = text(o["model"])
        // known_issues: [{area, why, source_title}] (or plain strings).
        knownIssues = (o["known_issues"] as? [Any] ?? []).compactMap { v in
            if let d = v as? [String: Any], let area = text(d["area"]) {
                return text(d["why"]).map { "\(area.capitalizedFirst): \(ReportSpeech.lowerFirst($0))" } ?? area.capitalizedFirst
            }
            return text(v)
        }
        issueAreas = (o["known_issues"] as? [[String: Any]] ?? []).compactMap { text($0["area"]) }
        category = text(o["category"])
        sources = (o["sources"] as? [Any] ?? []).compactMap { v -> String? in
            if let d = v as? [String: Any] { return text(d["title"]) ?? text(d["source_title"]) ?? (d["url"] as? String) }
            return text(v)
        }
        if name == nil, brand == nil, model == nil, category == nil { return nil }
    }

    /// What Lea says: the product, then one known issue (from public sources, not from the photos).
    var spoken: String {
        let what = [brand, model].compactMap { $0 }.joined(separator: " ")
        // The name alone when it already carries the brand ("Apple 20W USB-C adapter", not "Apple, Apple 20W…").
        let product: String
        if let name, let brand, name.localizedCaseInsensitiveContains(brand) { product = name }
        else { product = [what.isEmpty ? nil : what, name].compactMap { $0 }.joined(separator: ", ") }
        var out = "This looks like \(product.isEmpty ? (category ?? "it") : product)."
        if let area = issueAreas.first {
            out += " Owners often report trouble with the \(ReportSpeech.lowerFirst(area)), so we'll look closely there."
        } else if let issue = knownIssues.first {
            out += " A known issue: \(ReportSpeech.lowerFirst(ReportSpeech.firstSentence(issue)))"
        }
        return out
    }
}

/// The camera, for one photo of the label.
struct CameraPicker: UIViewControllerRepresentable {
    let done: (UIImage?) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController()
        p.sourceType = UIImagePickerController.isSourceTypeAvailable(.camera) ? .camera : .photoLibrary
        p.delegate = context.coordinator
        return p
    }

    func updateUIViewController(_ vc: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(done: done) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let done: (UIImage?) -> Void
        init(done: @escaping (UIImage?) -> Void) { self.done = done }
        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            done(info[.originalImage] as? UIImage)
        }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { done(nil) }
    }
}

private extension UIImage {
    /// Pixels drawn upright (the JPEG then needs no orientation tag).
    func normalizedUp() -> UIImage {
        guard imageOrientation != .up else { return self }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: pixelSize, format: format).image { _ in
            draw(in: CGRect(origin: .zero, size: pixelSize))
        }
    }

    var pixelSize: CGSize { CGSize(width: (size.width * scale).rounded(), height: (size.height * scale).rounded()) }
}
