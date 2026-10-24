import CryptoKit
import Foundation

/// Typed client for the local Lea bridge. The bridge is the only SpacetimeDB client; this app never
/// holds a database, Grain, Cartesia or Anthropic credential, only its paired-device token.
nonisolated final class BridgeClient: Sendable {
    enum Failure: LocalizedError {
        case notConfigured
        case http(Int, String)
        case transport(String)

        var errorDescription: String? {
            switch self {
            case .notConfigured: "Pair Lea with the bridge in Settings first."
            case .http(let status, let message): "Bridge \(status): \(message)"
            case .transport(let message): "Bridge unreachable: \(message)"
            }
        }
    }

    let baseURL: URL
    private let token: String
    private let session: URLSession
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = f.date(from: raw) { return date }
            f.formatOptions = [.withInternetDateTime]
            if let date = f.date(from: raw) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "bad date \(raw)"))
        }
        return d
    }()

    init(baseURL: URL, token: String) {
        self.baseURL = baseURL
        self.token = token
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 3600
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)
    }

    // MARK: Sessions and state

    func health() async throws -> BridgeHealth { try await get("/v1/health") }

    func createSession(id: String, title: String) async throws -> SessionSnapshot {
        try await send("POST", "/v1/sessions", ["id": id, "title": title])
    }

    func snapshot(_ sessionId: String) async throws -> SessionSnapshot { try await get("/v1/sessions/\(sessionId)") }

    // MARK: Evidence

    /// Uploads an original byte-for-byte. The bridge recomputes the SHA-256 and refuses a mismatch,
    /// and repeating an upload of the same asset id is safe.
    func uploadAsset(sessionId: String, assetId: String, kind: String, fileURL: URL, mediaType: String,
                     sha256: String, sourceAssetIds: [String] = [], meta: [String: String] = [:]) async throws -> RemoteAsset {
        var request = makeRequest("PUT", "/v1/sessions/\(sessionId)/assets/\(assetId)")
        request.setValue(mediaType, forHTTPHeaderField: "Content-Type")
        request.setValue(kind, forHTTPHeaderField: "x-lea-kind")
        request.setValue(sha256, forHTTPHeaderField: "x-lea-sha256")
        if !sourceAssetIds.isEmpty { request.setValue(sourceAssetIds.joined(separator: ","), forHTTPHeaderField: "x-lea-sources") }
        request.setValue(try JSONEncoder().encode(meta).base64EncodedString(), forHTTPHeaderField: "x-lea-meta")
        do {
            let (data, response) = try await session.upload(for: request, fromFile: fileURL)
            return try decode(data, response)
        } catch let error as Failure { throw error } catch { throw Failure.transport(error.localizedDescription) }
    }

    func assetData(sessionId: String, assetId: String) async throws -> Data {
        let request = makeRequest("GET", "/v1/sessions/\(sessionId)/assets/\(assetId)")
        do {
            let (data, response) = try await session.data(for: request)
            try check(data, response)
            // The bridge sends the digest it stored; a file that changed on disk is refused, not shown as evidence.
            if let expected = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "X-Lea-SHA256"),
               SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() != expected {
                throw Failure.http(0, "the downloaded file does not match its recorded hash")
            }
            return data
        } catch let error as Failure { throw error } catch { throw Failure.transport(error.localizedDescription) }
    }

    func pinReference(sessionId: String, assetId: String) async throws -> SessionSnapshot {
        try await send("POST", "/v1/sessions/\(sessionId)/reference", ["assetId": assetId])
    }

    // MARK: Reconstruction and comparison

    struct PolicyRequest: Encodable, Sendable {
        let scaleAnchorAxis: String
        let scaleAnchorMm: Double
        let holdoutAxis: String
        let holdoutMm: Double
        let maxHoldoutErrorMm: Double
        let minCoverage: Double
        let maxFitRmsMm: Double
        let minFitInlierShare: Double
    }

    func freezePolicy(sessionId: String, _ policy: PolicyRequest) async throws -> QAPolicy {
        try await send("POST", "/v1/sessions/\(sessionId)/qa-policies", policy)
    }

    struct ReconstructionRequest: Encodable, Sendable {
        let photoAssetIds: [String]
        let policyId: String
        let referenceUnitsPerMm: Double
        let autoDesignObjective: String?
        let meshAssetId: String?      // a model the phone built; nil = build it on the Mac from the photos
        let requestId: String
    }

    func startReconstruction(sessionId: String, _ request: ReconstructionRequest) async throws -> RemoteJob {
        try await send("POST", "/v1/sessions/\(sessionId)/reconstructions", request)
    }

    func meshView(sessionId: String, reportId: String) async throws -> MeshView {
        try await get("/v1/sessions/\(sessionId)/mesh-reports/\(reportId)/view")
    }

    func compareCloseup(sessionId: String, closeupAssetId: String, referencePhotoAssetId: String, subject: String, requestId: String) async throws -> RemoteJob {
        try await send("POST", "/v1/sessions/\(sessionId)/closeup-comparisons", [
            "closeupAssetId": closeupAssetId, "referencePhotoAssetId": referencePhotoAssetId, "subject": subject, "requestId": requestId,
        ])
    }

    /// Live coaching: one frame (never stored) judged by Jev, optionally against a saved reference photo.
    func look(sessionId: String, frame: Data, subject: String, referenceAssetId: String?) async throws -> LookResult {
        var request = makeRequest("POST", "/v1/sessions/\(sessionId)/look", timeout: 10)
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.setValue(subject.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "", forHTTPHeaderField: "x-lea-subject")
        if let referenceAssetId { request.setValue(referenceAssetId, forHTTPHeaderField: "x-lea-reference") }
        do {
            let (data, response) = try await session.upload(for: request, from: frame)
            return try decode(data, response)
        } catch let error as Failure { throw error } catch { throw Failure.transport(error.localizedDescription) }
    }

    // MARK: Repair

    struct CheckRequest: Encodable, Sendable {
        let purpose: String
        let instruction: String
        let safety: String
        let powerState: String
        let expectedMin: Double?
        let expectedMax: Double?
        let expectedUnit: String
        let expectedSource: String
        var net: String = ""            // the circuit, when the range came from the design
    }

    struct NextChecks: Decodable, Sendable { let ranked: Bool; let reason: String?; let order: [String] }

    /// Pending checks in the order to do them (Jev-ranked; `ranked` false with reason unable_to_rank otherwise).
    func nextChecks(sessionId: String) async throws -> NextChecks { try await get("/v1/sessions/\(sessionId)/next-checks") }

    func addCheck(sessionId: String, findingId: String, _ check: CheckRequest) async throws -> RepairCheck {
        try await send("POST", "/v1/sessions/\(sessionId)/findings/\(findingId)/checks", check)
    }

    struct OutcomeRequest: Encodable, Sendable {
        let observed: String
        let observationAssetId: String?
        let readingValue: Double?
        let readingUnit: String?
        let note: String?
    }

    func recordOutcome(sessionId: String, checkId: String, _ outcome: OutcomeRequest) async throws -> SessionSnapshot {
        try await send("POST", "/v1/sessions/\(sessionId)/checks/\(checkId)/outcome", outcome)
    }

    func recordRepair(sessionId: String, findingId: String, action: String, verifyCheckId: String) async throws -> SessionSnapshot {
        try await send("POST", "/v1/sessions/\(sessionId)/repairs", ["findingId": findingId, "action": action, "verifyCheckId": verifyCheckId])
    }

    // MARK: Grain and voice

    func linkGrain(sessionId: String, projectId: String?, createName: String?) async throws -> SessionSnapshot {
        var body: [String: String] = [:]
        if let projectId { body["grainProjectId"] = projectId }
        if let createName { body["createName"] = createName }
        return try await send("POST", "/v1/sessions/\(sessionId)/grain", body)
    }

    func grainLive(sessionId: String) async throws -> GrainLive { try await get("/v1/sessions/\(sessionId)/grain") }

    /// A dimension the returned STEP must meet (checked on a re-imported solid).
    struct DimensionCheck: Codable, Sendable, Hashable { var axis: String; var mm: Double; var tolMm: Double }

    func submitDesign(sessionId: String, text: String, origin: String, meshReportId: String?, requestId: String, checks: [DimensionCheck] = []) async throws -> DesignCommand {
        struct Body: Encodable { let text, origin, requestId: String; let meshReportId: String?; let checks: [DimensionCheck] }
        return try await send("POST", "/v1/sessions/\(sessionId)/design-commands",
                              Body(text: text, origin: origin, requestId: requestId, meshReportId: meshReportId, checks: checks))
    }

    /// Pins the linked Grain design's built model as this session's reference.
    func referenceFromGrain(sessionId: String) async throws -> SessionSnapshot {
        try await send("POST", "/v1/sessions/\(sessionId)/reference-from-grain", [String: String]())
    }

    /// Expected voltages per net from the linked design, for sourced meter ranges.
    func grainElectrical(sessionId: String) async throws -> GrainNets { try await get("/v1/sessions/\(sessionId)/grain/electrical") }

    func cartesiaGrant() async throws -> CartesiaGrant { try await send("POST", "/v1/voice/token", [String: String]()) }

    /// A final spoken turn, with the camera view if one is open (seen by the agent for this turn only, never stored).
    /// focus: a finding the person selected (on the 3D model), so "what's that?" has a referent.
    func recordVoiceTurn(sessionId: String, id: String, mode: String, transcript: String, frame: Data? = nil, focus: String? = nil) async throws -> VoiceTurnResult {
        var body = ["id": id, "mode": mode, "transcript": transcript]
        if let frame { body["frame"] = frame.base64EncodedString() }
        if let focus { body["focus"] = focus }
        return try await send("POST", "/v1/sessions/\(sessionId)/voice-turns", body)
    }

    /// Ordered change notifications after `after`; the caller refetches the snapshot on each one.
    func events(sessionId: String, after: Int) -> AsyncThrowingStream<SessionEvent, Error> {
        let request = makeRequest("GET", "/v1/sessions/\(sessionId)/events?after=\(after)", timeout: 3600)
        let session = session
        let decoder = decoder
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw Failure.http((response as? HTTPURLResponse)?.statusCode ?? 0, "event stream refused") }
                    for try await line in bytes.lines where line.hasPrefix("data: ") {
                        if let event = try? decoder.decode(SessionEvent.self, from: Data(line.dropFirst(6).utf8)) { continuation.yield(event) }
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Plumbing

    private func makeRequest(_ method: String, _ path: String, timeout: TimeInterval = 60) -> URLRequest {
        var request = URLRequest(url: URL(string: path, relativeTo: baseURL)!, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let request = makeRequest("GET", path)
        do {
            let (data, response) = try await session.data(for: request)
            return try decode(data, response)
        } catch let error as Failure { throw error } catch let error as DecodingError { throw Failure.transport("unexpected response: \(error)") } catch { throw Failure.transport(error.localizedDescription) }
    }

    private func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, _ body: B) async throws -> T {
        var request = makeRequest(method, path)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        do {
            let (data, response) = try await session.data(for: request)
            return try decode(data, response)
        } catch let error as Failure { throw error } catch let error as DecodingError { throw Failure.transport("unexpected response: \(error)") } catch { throw Failure.transport(error.localizedDescription) }
    }

    /// Replays a request saved while offline (body already JSON-encoded).
    func sendRaw(_ method: String, _ path: String, json: Data) async throws {
        var request = makeRequest(method, path)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = json
        do {
            let (data, response) = try await session.data(for: request)
            try check(data, response)
        } catch let error as Failure { throw error } catch { throw Failure.transport(error.localizedDescription) }
    }

    /// The JSON body a typed call would send, for saving it while offline.
    static func encodedBody<B: Encodable>(_ body: B) -> Data { (try? JSONEncoder().encode(body)) ?? Data("{}".utf8) }

    private func check(_ data: Data, _ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else { throw Failure.transport("no HTTP response") }
        guard (200..<300).contains(http.statusCode) else {
            let message = (try? JSONDecoder().decode(BridgeErrorBody.self, from: data))?.error.message ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            throw Failure.http(http.statusCode, message)
        }
    }

    private func decode<T: Decodable>(_ data: Data, _ response: URLResponse) throws -> T {
        try check(data, response)
        return try decoder.decode(T.self, from: data)
    }
}
