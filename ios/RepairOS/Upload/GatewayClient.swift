import CryptoKit
import Foundation

enum GatewayError: LocalizedError {
    case message(String)
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .message(let m): m
        case .http(422, let body): "The server rejected the scan: \(Self.firstProblem(body))"
        case .http(401, _), .http(403, _): "The server doesn't recognise this phone: connect again in Settings."
        case .http(let code, let body): "The server answered \(code): \(Self.firstProblem(body))"
        }
    }

    /// The first human-readable problem in a gateway error body (FastAPI `detail`, our `problems` list, or a
    /// schema `message`), else the start of the body.
    static func firstProblem(_ body: String) -> String {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any] else {
            return String(body.prefix(200))
        }
        let detail = obj["detail"]
        if let d = detail as? [String: Any] {
            if let p = (d["problems"] as? [Any])?.first { return "\(p)" }
            if let m = d["message"] as? String { return m }
            if let e = d["error"] as? String { return e }
        }
        if let d = detail as? String { return d }
        if let list = detail as? [[String: Any]], let first = list.first {
            let loc = (first["loc"] as? [Any])?.map { "\($0)" }.joined(separator: ".") ?? ""
            return "\(first["msg"] ?? "invalid") \(loc)".trimmingCharacters(in: .whitespaces)
        }
        if let m = obj["message"] as? String { return m }
        return String(body.prefix(200))
    }
}

/// The phone's only network peer (backend/repairos/api/app.py). Errors surface verbatim; 422 problem lists are
/// human-readable.
struct GatewayClient: Sendable {
    let baseURL: URL
    let token: String?
    static let chunkSize = 4 << 20

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 60
        c.waitsForConnectivity = true
        c.timeoutIntervalForResource = 600
        return URLSession(configuration: c)
    }()

    func pair(code: String, name: String) async throws -> String {
        let (data, _) = try await send("POST", "v1/devices/pair", json: ["code": code, "name": name], auth: false,
                                       expect: [201])
        guard let token = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["token"] as? String else {
            throw GatewayError.message("Pairing answer had no token.")
        }
        return token
    }

    func health() async throws {
        _ = try await send("GET", "healthz", auth: false, expect: [200])
    }

    func createSession(symptom: String?) async throws -> String {
        var body: [String: Any] = [:]
        if let symptom { body["symptom"] = symptom }
        let (data, _) = try await send("POST", "v1/sessions", json: body, expect: [201])
        guard let id = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["session_id"] as? String else {
            throw GatewayError.message("Session answer had no session_id.")
        }
        return id
    }

    /// 201 on the first post (or an identical retry), 200 `updated: true` on a changed re-post of the same scan_id.
    func postScan(_ body: Data) async throws -> [String: Any] {
        let (data, _) = try await send("POST", "v1/scans", body: body, expect: [200, 201])
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
    /// 201; `scan_pending: true` when the still arrived before its scan package (inspection starts anyway).
    func postStill(_ body: Data) async throws -> [String: Any] {
        let (data, _) = try await send("POST", "v1/stills", body: body, expect: [200, 201])
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
    func complete(scanID: String) async throws {
        _ = try await send("POST", "v1/scans/\(scanID)/complete", json: [:], expect: [202])
    }

    /// GET /v1/scans/{id}/pipeline: `status` is not_started, running, done or failed.
    func pipeline(scanID: String) async throws -> [String: Any] {
        let (data, _) = try await send("GET", "v1/scans/\(scanID)/pipeline", expect: [200])
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func report(scanID: String) async throws -> [String: Any] {
        try await reportWithRev(scanID: scanID).report
    }

    /// The report and its `X-Report-Rev`. 404 while a rebuild is in progress.
    func reportWithRev(scanID: String) async throws -> (report: [String: Any], rev: Int?) {
        let (data, http) = try await send("GET", "v1/scans/\(scanID)/report", expect: [200])
        let rev = (http.value(forHTTPHeaderField: "X-Report-Rev")).flatMap(Int.init)
        return ((try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:], rev)
    }

    /// GET /v1/scans/{id}/mesh: the scan's mesh (binary PLY, the scan's ARKit frame). 404 until it is built.
    func mesh(scanID: String) async throws -> Data {
        try await send("GET", "v1/scans/\(scanID)/mesh", expect: [200]).0
    }

    /// GET /v1/scans/{id}/cloud: the object's points, cleaned by the backend (binary PLY, the scan's ARKit frame).
    func cloud(scanID: String) async throws -> Data {
        try await send("GET", "v1/scans/\(scanID)/cloud", expect: [200]).0
    }

    /// GET /v1/scans/{id}/recreation: the photo-real recreation (Report/Recreation), its kind, revision, file format
    /// and the object's centre + radius for framing (`X-Recreation-*`). 404 until it is built. `kind: "splat"` asks
    /// for the Gaussian splat (404 until the GPU pod trained one); nil, the textured mesh.
    func recreation(scanID: String, kind: String? = nil) async throws -> (data: Data, kind: String, rev: Int, format: String, focusCenter: [Float]?, focusRadius: Float?) {
        let query = kind.map { "?kind=\($0)" } ?? ""
        let (data, http) = try await send("GET", "v1/scans/\(scanID)/recreation\(query)", expect: [200])
        let focus = http.value(forHTTPHeaderField: "X-Recreation-Focus")?.split(separator: ",").compactMap { Float($0) }
        return (data, http.value(forHTTPHeaderField: "X-Recreation-Kind") ?? "", Int(http.value(forHTTPHeaderField: "X-Recreation-Rev") ?? "") ?? 0,
                http.value(forHTTPHeaderField: "X-Recreation-Format") ?? "", focus.map { Array($0.prefix(3)) },
                focus.flatMap { $0.count == 4 ? $0[3] : nil })
    }

    /// GET /v1/sessions/{id}/events (server-sent events), resuming after `lastEventID`.
    func eventsRequest(sessionID: String, lastEventID: String?) -> URLRequest {
        var req = request("GET", "v1/sessions/\(sessionID)/events", auth: true)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let lastEventID { req.setValue(lastEventID, forHTTPHeaderField: "Last-Event-ID") }
        req.timeoutInterval = 40
        return req
    }

    /// GET /v1/events (server-sent events that belong to no session: `spend_alert`).
    func globalEventsRequest() -> URLRequest {
        var req = request("GET", "v1/events", auth: true)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.timeoutInterval = 40
        return req
    }

    /// GET /v1/usage: paid-service usage on the Mac (per service: calls, estimated USD, caps, pauses).
    func usage() async throws -> [String: Any] {
        let (data, _) = try await send("GET", "v1/usage", expect: [200])
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func reportHTMLRequest(scanID: String) -> URLRequest {
        request("GET", "v1/scans/\(scanID)/report/export?format=html", auth: true)
    }

    // MARK: - Blobs (resumable)

    /// Uploads a file as a content-addressed blob and returns its ref. Resumes from the gateway's staged offset
    /// (HEAD → Upload-Offset), so a cut connection or a killed app never re-sends finished chunks.
    func uploadBlob(file: URL, sha256: String, contentType: String) async throws -> String {
        let total = try FileManager.default.attributesOfItem(atPath: file.path)[.size] as? Int ?? 0
        var offset = 0
        let head = request("HEAD", "v1/blobs/\(sha256)", auth: true)
        let (_, headResp) = try await Self.session.data(for: head)
        let headHTTP = headResp as? HTTPURLResponse
        if headHTTP?.value(forHTTPHeaderField: "Upload-Complete") == "1" { return "blob://sha256:\(sha256)" }
        offset = Int(headHTTP?.value(forHTTPHeaderField: "Upload-Offset") ?? "0") ?? 0

        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var attempts = 0
        while true {
            try handle.seek(toOffset: UInt64(offset))
            let chunk = try handle.read(upToCount: Self.chunkSize) ?? Data()
            var req = request("PUT", "v1/blobs/\(sha256)", auth: true)
            req.setValue(contentType, forHTTPHeaderField: "Content-Type")
            if total > 0 {
                req.setValue("bytes \(offset)-\(offset + chunk.count - 1)/\(total)", forHTTPHeaderField: "Content-Range")
            }
            let (data, resp) = try await Self.session.upload(for: req, from: chunk)
            let http = resp as! HTTPURLResponse
            switch http.statusCode {
            case 200, 201:
                return "blob://sha256:\(sha256)"
            case 202:
                offset = Int(http.value(forHTTPHeaderField: "Upload-Offset") ?? "") ?? offset + chunk.count
            case 409:
                attempts += 1
                guard attempts < 5, let o = Int(http.value(forHTTPHeaderField: "Upload-Offset") ?? "") else {
                    throw GatewayError.http(409, String(decoding: data, as: UTF8.self))
                }
                offset = o
            default:
                throw GatewayError.http(http.statusCode, String(decoding: data, as: UTF8.self))
            }
        }
    }

    static func sha256(of file: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 1 << 20), !data.isEmpty { hasher.update(data: data) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - Plumbing

    private func request(_ method: String, _ path: String, auth: Bool) -> URLRequest {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        let parts = path.split(separator: "?", maxSplits: 1)
        comps.path = (comps.path.hasSuffix("/") ? String(comps.path.dropLast()) : comps.path) + "/" + parts[0]
        comps.percentEncodedQuery = parts.count > 1 ? String(parts[1]) : nil
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        if auth, let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func send(_ method: String, _ path: String, json: [String: Any]? = nil, body: Data? = nil,
                      auth: Bool = true, expect: Set<Int>) async throws -> (Data, HTTPURLResponse) {
        var req = request(method, path, auth: auth)
        if let payload = try body ?? json.map({ try JSONSerialization.data(withJSONObject: $0) }) {
            req.httpBody = payload
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await Self.session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw GatewayError.message("No HTTP answer.") }
        guard expect.contains(http.statusCode) else {
            throw GatewayError.http(http.statusCode, String(decoding: data, as: UTF8.self))
        }
        return (data, http)
    }
}
