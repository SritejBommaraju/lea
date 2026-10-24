import Foundation

/// One local repair session on the phone. The folder uses the replayable `samples/` layout (samples/README.md):
/// `scans/<scan_id>/scan_package.json` + files, `stills/<still_id>.json` + files. File fields in those JSONs are
/// paths relative to the session folder until the upload swaps them for blob refs.
struct SessionRecord: Codable, Sendable, Identifiable {
    enum Status: String, Codable, Sendable { case capturing, captured, uploading, uploaded, reported, failed }

    var id: String                // local session id (folder name)
    var createdAt: Date
    var symptom: String?
    var productID: String
    var scanID: String
    var engine: EngineKind
    var stillIDs: [String] = []
    var status: Status = .capturing
    var gatewaySessionID: String?
    var lastError: String?
    /// Capture segments so far (nil = 1). "Continue scanning" adds the next one.
    var segmentCount: Int?
    /// What the person said the object is (correcting the backend's live guess), when they did.
    var objectName: String?
}

/// Local session folders under Documents/sessions (visible in the Files app for debugging).
struct ScanStorage: Sendable {
    let root: URL

    init(root: URL? = nil) {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        self.root = root ?? docs.appending(path: "sessions", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: self.root, withIntermediateDirectories: true)
    }

    func newSession(symptom: String?, engine: EngineKind) throws -> SessionRecord {
        let stamp = ISO8601DateFormatter.compact.string(from: Date())
        let record = SessionRecord(id: "sess_local_\(stamp)_\(Self.hex(4))", createdAt: Date(),
                                   symptom: symptom?.trimmedOrNil, productID: "prod_\(Self.hex(8))",
                                   scanID: "scan_\(Self.hex(8))", engine: engine)
        try FileManager.default.createDirectory(at: scanDir(record), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: stillsDir(record), withIntermediateDirectories: true)
        try save(record)
        return record
    }

    func dir(_ r: SessionRecord) -> URL { root.appending(path: r.id, directoryHint: .isDirectory) }
    func scanDir(_ r: SessionRecord) -> URL { dir(r).appending(path: "scans/\(r.scanID)", directoryHint: .isDirectory) }
    func stillsDir(_ r: SessionRecord) -> URL { dir(r).appending(path: "stills", directoryHint: .isDirectory) }
    func scanPackageURL(_ r: SessionRecord) -> URL { scanDir(r).appending(path: "scan_package.json") }
    func stillURL(_ r: SessionRecord, _ stillID: String) -> URL { stillsDir(r).appending(path: "\(stillID).json") }

    /// Path of `url` relative to the session folder, as written into the JSON file fields.
    func relative(_ url: URL, in r: SessionRecord) -> String {
        String(url.standardizedFileURL.path.dropFirst(dir(r).standardizedFileURL.path.count + 1))
    }

    func save(_ r: SessionRecord) throws {
        try JSON.encoder.encode(r).write(to: dir(r).appending(path: "app_session.json"), options: .atomic)
    }

    func listSessions() -> [SessionRecord] {
        let dirs = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        return dirs.compactMap { d in
            (try? Data(contentsOf: d.appending(path: "app_session.json"))).flatMap {
                try? JSON.decoder.decode(SessionRecord.self, from: $0)
            }
        }.sorted { $0.createdAt > $1.createdAt }
    }

    /// False for the session being captured right now.
    @MainActor
    func canDelete(_ r: SessionRecord) -> Bool { CaptureRegistry.active != r.id }

    /// Deletes a session's folder and cancels its uploads; does nothing for the session being captured.
    @MainActor
    func delete(_ r: SessionRecord) {
        guard canDelete(r) else { return }
        UploadQueue.cancel(sessionID: r.id)
        try? FileManager.default.removeItem(at: dir(r))
    }

    /// Deletes every session except the one being captured; returns how many were deleted.
    @MainActor @discardableResult
    func deleteAll() -> Int {
        let doomed = listSessions().filter(canDelete)
        doomed.forEach(delete)
        return doomed.count
    }

    /// Bytes used by all sessions on the phone.
    func usedBytes() -> Int64 {
        guard let walker = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.totalFileAllocatedSizeKey, .fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let url as URL in walker {
            let v = try? url.resourceValues(forKeys: [.totalFileAllocatedSizeKey, .fileSizeKey])
            total += Int64(v?.totalFileAllocatedSize ?? v?.fileSize ?? 0)
        }
        return total
    }

    static func hex(_ bytes: Int) -> String {
        (0..<bytes).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    }
}

/// JSON coders for the contracts: ISO 8601 dates, absent optionals omitted, stable key order.
enum JSON {
    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()
}

extension ISO8601DateFormatter {
    nonisolated(unsafe) static let compact: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withYear, .withMonth, .withDay, .withTime]
        return f
    }()
}

extension String {
    var trimmedOrNil: String? {
        let t = trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }
}
