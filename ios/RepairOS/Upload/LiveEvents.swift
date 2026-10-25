import Foundation
import Observation

/// The gateway's live events for one session (GET /v1/sessions/{id}/events, server-sent events), the channel that
/// carries `report_ready`. While scanning, the app shows what the backend thinks the object is (`identified`) and how
/// many areas it has checked (`progress`); the report refetches the 3D mesh on `mesh_ready` and shows the "Fix it"
/// offers on `fix_ready`. Reconnects with
/// Last-Event-ID, so nothing is missed. State changes only when an event arrives (a few a second at most).
@MainActor
@Observable
final class LiveEvents {
    struct Identity: Equatable, Sendable {
        var category: String
        var brand: String?
        var model: String?
        var confidence: String?

        /// "Category · Brand Model", or the category alone, in the backend's own generic words (as the report header).
        var words: String {
            ReportModel.objectWords(["category": category, "brand": brand as Any, "model": model as Any]) ?? category.capitalizedFirst
        }
    }

    private(set) var identity: Identity?
    /// What the person said it is instead (tap to correct); shown in place of `identity`.
    private(set) var corrected: String?
    private(set) var areasChecked: Int?
    private(set) var meshRev: Int?
    /// The backend's hero choice with that mesh revision (points, mesh, photos).
    private(set) var hero: String?
    /// The gateway's photo-real recreation revision (`recreation_ready`; Report/Recreation fetches it).
    private(set) var recreationRev: Int?
    private(set) var reportRev: Int?
    /// "Fix it" offers for the top finding's part (`fix_ready`), when the search finished after the report.
    private(set) var fix: FixIt?
    /// The search ended with no offers (`fix_ready` with a status only): none, timeout, failed, or `limited` (the
    /// spend cap: "not available right now").
    private(set) var fixEnded: String?
    private(set) var connected = false
    /// The latest `product_brief`: the product and its known issues, from public sources.
    private(set) var brief: ProductBrief?
    /// The latest lead (`lead` event): an area worth a close look because of what the person said is wrong.
    private(set) var lead: Lead?

    struct Lead: Equatable, Sendable {
        var area: String
        var why: String?
        var sourceTitle: String?
        /// `you`: the person's own suspect; `web`: a research lead; `product`: a known failure point of the product
        /// (the pre-scan product brief).
        var from: String = "you"
        /// Said and shown as a scan cue; the area words are the backend's (from what the person said).
        var spoken: String {
            from == "you" ? "You mentioned the \(area). Let's get a close look at it." : "Worth a close look: the \(area)."
        }
    }

    let scanID: String
    private var lastEventID: String?
    private var task: Task<Void, Never>?

    /// Where arrivals are logged (`live_event`, ms since the stream opened), when set.
    var diag: Diag?
    private var openedAt: Date?

    init(scanID: String, corrected: String? = nil, diag: Diag? = nil) {
        self.scanID = scanID
        self.corrected = corrected
        self.diag = diag
    }

    func correct(_ words: String?) { corrected = words?.trimmedOrNil }

    /// Listens until `stop()`; `sessionID` is asked for again until the upload has created the gateway session.
    func start(gateway: GatewayClient, sessionID: @escaping @MainActor () -> String?) {
        guard task == nil else { return }
        task = Task { [weak self] in
            var backoff: Double = 1
            while !Task.isCancelled {
                guard let self else { return }
                guard let sid = sessionID() else {
                    try? await Task.sleep(for: .seconds(1))
                    continue
                }
                do {
                    try await self.listen(gateway.eventsRequest(sessionID: sid, lastEventID: self.lastEventID))
                    backoff = 1
                } catch is CancellationError {
                    return
                } catch {
                    self.connected = false
                    try? await Task.sleep(for: .seconds(backoff))
                    backoff = min(8, backoff * 2)
                }
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        connected = false
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 40       // the gateway sends a keepalive every 15 s
        c.timeoutIntervalForResource = 24 * 3600
        return URLSession(configuration: c)
    }()

    private func listen(_ request: URLRequest) async throws {
        let (bytes, response) = try await Self.session.bytes(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw GatewayError.message("events unavailable") }
        connected = true
        if openedAt == nil { openedAt = Date() }
        diag?.log("live_events_open", [:])
        var parser = SSEParser()
        // Split lines from the raw bytes: `bytes.lines` drops the blank line that ends each event, which would hold
        // every event back until the next one arrives.
        var line: [UInt8] = []
        for try await byte in bytes {
            if byte == 0x0A {
                if line.last == 0x0D { line.removeLast() }
                if let event = parser.feed(String(decoding: line, as: UTF8.self)) { handle(event) }
                line.removeAll(keepingCapacity: true)
            } else {
                line.append(byte)
            }
        }
        if let event = parser.flush() { handle(event) }
        connected = false
    }

    func handle(_ e: SSEParser.Event) {
        if let id = e.id { lastEventID = id }
        guard let obj = (try? JSONSerialization.jsonObject(with: Data(e.data.utf8))) as? [String: Any] else { return }
        if let sid = obj["scan_id"] as? String, sid != scanID { return }
        if ["identified", "progress", "mesh_ready", "report_ready", "fix_ready", "lead"].contains(e.name) {
            diag?.log("live_event", ["name": e.name, "after_open_ms": openedAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1,
                                     "areas_checked": obj["areas_checked"] as? Int ?? -1, "confidence": obj["confidence"] as? String ?? ""])
        }
        switch e.name {
        case "identified":
            guard let category = obj["category"] as? String, category != "unknown" else { return }
            let next = Identity(category: category, brand: obj["brand"] as? String, model: obj["model"] as? String,
                                confidence: obj["confidence"] as? String)
            if next != identity { identity = next }
        case "progress":
            if let n = obj["areas_checked"] as? Int, n != areasChecked { areasChecked = n }
        case "mesh_ready":
            if let h = obj["hero"] as? String, h != hero { hero = h }
            if let rev = obj["mesh_rev"] as? Int, rev != meshRev { meshRev = rev }
        case "report_ready":
            if let rev = obj["rev"] as? Int { reportRev = rev }
        case "recreation_ready":
            if let rev = obj["rev"] as? Int, rev != recreationRev { recreationRev = rev }
        case "product_brief":   // what the object is, from public sources (Voice/PreScan says it before the scan)
            if let b = ProductBrief(json: obj), b != brief { brief = b }
        case "lead":   // the person's guess, researched by the backend (Voice/ says it during the scan)
            if let area = (obj["area"] as? String)?.trimmingCharacters(in: .whitespaces), !area.isEmpty {
                let next = Lead(area: area, why: obj["why"] as? String, sourceTitle: obj["source_title"] as? String,
                                from: obj["from"] as? String ?? "you")
                if next != lead { lead = next }
            }
        case "spend_alert":   // a paid service on the Mac is paused or near its limit: an app-wide banner
            UsageAlerts.shared.receive(obj)
        case "fix_ready":
            if let next = FixIt(json: obj) {
                if next != fix { fix = next }
            } else {
                fixEnded = obj["status"] as? String ?? "none"
            }
        default:
            break
        }
    }
}

/// Server-sent events, line by line (https://html.spec.whatwg.org/multipage/server-sent-events.html): `event:`,
/// `data:` (joined with newlines), `id:`; an event ends at a blank line (or, defensively, at a new `event:`/`id:`
/// after a `data:` line).
struct SSEParser {
    struct Event: Equatable { var name: String; var data: String; var id: String? }

    private var name = "message"
    private var data: [String] = []
    private var id: String?

    mutating func feed(_ line: String) -> Event? {
        if line.isEmpty { return flush() }
        if line.hasPrefix(":") { return nil }
        let (field, value) = Self.split(line)
        var out: Event?
        if (field == "event" || field == "id"), !data.isEmpty { out = flush() }
        switch field {
        case "event": name = value
        case "data": data.append(value)
        case "id": id = value
        default: break
        }
        return out
    }

    mutating func flush() -> Event? {
        defer { name = "message"; data = []; id = nil }
        guard !data.isEmpty else { return nil }
        return Event(name: name, data: data.joined(separator: "\n"), id: id)
    }

    private static func split(_ line: String) -> (String, String) {
        guard let i = line.firstIndex(of: ":") else { return (line, "") }
        var value = line[line.index(after: i)...]
        if value.hasPrefix(" ") { value = value.dropFirst() }
        return (String(line[..<i]), String(value))
    }
}
