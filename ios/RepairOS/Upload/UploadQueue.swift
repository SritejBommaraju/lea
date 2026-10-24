import Foundation
import Observation

/// Uploads one session in the background while the person is still taking close-ups: scan files + scan package
/// first, then each still as soon as it is saved, then `complete`. State is persisted in `upload.json` in the
/// session folder, so a relaunch resumes where it stopped (blobs resume chunk-wise in `GatewayClient`).
@MainActor
@Observable
final class UploadQueue {
    struct State: Codable {
        var gatewaySessionID: String?
        var refs: [String: String] = [:]      // relative path -> blob ref
        /// relative path -> the file's size and modification time when its ref was made: a file rewritten since (a
        /// continuation's merged cloud keeps its name) is uploaded again.
        var stamps: [String: String]?

        /// The ref made from this very file (same stamp), else nil: the file is new or was rewritten since.
        func cachedRef(_ path: String, stamp: String) -> String? {
            guard let ref = refs[path], stamps?[path] == stamp else { return nil }
            return ref
        }
        var scanPosted = false
        /// The scan was posted at least once (a continuation's stills can go before its re-post).
        var everPosted: Bool?
        var stillsPosted: [String] = []
        var completed = false
    }

    enum Item: Equatable { case scan, still(String) }

    private(set) var state: State
    private(set) var pending: [Item] = []
    private(set) var busy = false
    private(set) var lastError: String?
    private(set) var uploadedFiles = 0

    private var record: SessionRecord
    private let storage: ScanStorage
    private let gateway: GatewayClient
    private var worker: Task<Void, Never>?
    private var doneAt: Date?
    private var drainedLogged = false
    private(set) var bytesSent: Int64 = 0
    /// Files to send ahead of their JSON (recorded keyframes), so little is left to upload after Done.
    private var ahead: [String] = []
    private var cancelled = false

    private struct Weak { weak var queue: UploadQueue? }
    private static var live: [String: Weak] = [:]

    /// Stops a session's uploads (its folder is being deleted): no request after this, nothing persisted.
    static func cancel(sessionID: String) {
        live[sessionID]?.queue?.cancel()
        live[sessionID] = nil
    }

    func cancel() {
        cancelled = true
        worker?.cancel()
        pending = []
        ahead = []
    }

    func sendAhead(_ paths: [String]) {
        guard !cancelled else { return }
        ahead += paths.filter { state.refs[$0] == nil }
        kick()
    }
    private let diag: Diag

    init(record: SessionRecord, storage: ScanStorage, gateway: GatewayClient) {
        self.record = record
        self.storage = storage
        self.gateway = gateway
        diag = Diag.session(storage.dir(record))
        let saved = (try? Data(contentsOf: storage.dir(record).appending(path: "upload.json")))
            .flatMap { try? JSON.decoder.decode(State.self, from: $0) }
        state = saved ?? State()
        Self.live[record.id] = Weak(queue: self)
    }

    var isIdle: Bool { pending.isEmpty && !busy }

    /// Queues everything not yet posted (used on resume and when the scan finishes).
    func enqueueAll(stillIDs: [String], scanReady: Bool) {
        if scanReady, !state.scanPosted, !pending.contains(.scan) { pending.append(.scan) }
        for id in stillIDs where !state.stillsPosted.contains(id) && !pending.contains(.still(id)) {
            pending.append(.still(id))
        }
        kick()
    }

    func enqueueStill(_ id: String) {
        guard !state.stillsPosted.contains(id), !pending.contains(.still(id)) else { return }
        pending.append(.still(id))
        kick()
    }

    func retry() {
        lastError = nil
        kick()
    }

    /// Waits for the queue to drain, then tells the gateway the capture is complete (queues the pipeline).
    func complete() async throws {
        while !isIdle {
            if let lastError { throw GatewayError.message(lastError) }
            try await Task.sleep(for: .milliseconds(200))
        }
        if let lastError { throw GatewayError.message(lastError) }
        if !state.completed {
            try await gateway.complete(scanID: record.scanID)
            state.completed = true
            persist()
        }
    }

    private func kick() {
        guard !cancelled, worker == nil, lastError == nil, !pending.isEmpty || !ahead.isEmpty else { return }
        worker = Task { [weak self] in
            await self?.drain()
            self?.worker = nil
        }
    }

    /// Stills first (the backend inspects each one as soon as it lands, before Done), then the scan package once it
    /// exists, then recorded frames whenever nothing else waits.
    private func drain() async {
        busy = true
        defer { busy = false }
        while !cancelled {
            // Stills post as soon as their files are up, during the scan (the gateway takes them before their scan
            // package, `scan_pending`, and starts inspecting). The scan package goes when it exists (at Done).
            let next = pending.first { if case .still = $0 { true } else { false } } ?? pending.first
            if let item = next, let i = pending.firstIndex(of: item) {
                _ = i
                var tries = 0
                while true {
                    do {
                        try await upload(item)
                        if let j = pending.firstIndex(of: item) { pending.remove(at: j) }
                        break
                    } catch let error as URLError where tries < 4 {
                        tries += 1
                        try? await Task.sleep(for: .seconds(Double(1 << tries)))
                        _ = error
                    } catch {
                        lastError = Self.words(error)
                        diag.log("upload_error", ["item": "\(item)", "message": lastError ?? "", "body": Self.body(error)])
                        return
                    }
                }
                continue
            }
            if let path = ahead.first {
                do {
                    if state.gatewaySessionID == nil {
                        state.gatewaySessionID = try await gateway.createSession(symptom: record.symptom)
                        persist()
                    }
                    _ = try await ref(path)
                    ahead.removeFirst()
                } catch {
                    break    // network trouble: these go again with the scan package
                }
                continue
            }
            break
        }
        if pending.isEmpty, state.scanPosted, let doneAt, !drainedLogged {
            drainedLogged = true
            diag.log("upload_drained", ["since_done_ms": Int(Date().timeIntervalSince(doneAt) * 1000),
                                        "bytes_total": bytesSent, "files": uploadedFiles, "frames_left": ahead.count])
        }
    }

    /// "Continue scanning" finished: post the scan package again (same scan_id, new segment; the gateway answers
    /// 200 `updated`) after the new stills, and ask for a fresh pipeline run.
    func repostScan() {
        state.scanPosted = false
        state.completed = false
        drainedLogged = false
        persist()
        if !pending.contains(.scan) { pending.append(.scan) }
        kick()
    }

    /// The person tapped Done: from here, the time until everything is sent is what counts.
    func markDone() {
        doneAt = Date()
        diag.log("upload_at_done", ["pending": pending.count, "frames_left": ahead.count, "bytes_so_far": bytesSent])
    }

    static func words(_ error: Error) -> String {
        if error is URLError { return "Can't reach the lea server: check the connection in Settings." }
        return error.localizedDescription
    }

    static func body(_ error: Error) -> String {
        if case GatewayError.http(_, let b) = error { return String(b.prefix(4000)) }
        return ""
    }

    private func upload(_ item: Item) async throws {
        if state.gatewaySessionID == nil {
            state.gatewaySessionID = try await gateway.createSession(symptom: record.symptom)
            persist()
        }
        switch item {
        case .scan:
            var body = try await Self.withRefs(loadJSON(storage.scanPackageURL(record)), ref)
            body["session_id"] = state.gatewaySessionID
            let data = try JSONSerialization.data(withJSONObject: body)
            let answer = try await gateway.postScan(data)
            diag.log("scan_posted", ["updated": answer["updated"] ?? NSNull(), "rev": answer["rev"] ?? NSNull(),
                                     "keyframes": (body["keyframes"] as? [Any])?.count ?? 0,
                                     "since_done_ms": doneAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1])
            state.scanPosted = true
            state.everPosted = true
        case .still(let id):
            var body = try loadJSON(storage.stillURL(record, id))
            body["session_id"] = state.gatewaySessionID
            for key in ["image_file", "depth_file", "confidence_file"] { body[key] = try await ref(body[key]) }
            let data = try JSONSerialization.data(withJSONObject: body)
            let answer = try await gateway.postStill(data)
            diag.log("still_posted", ["id": id, "scan_pending": answer["scan_pending"] ?? false, "segment": body["segment"] ?? 0,
                                      "during_scan": doneAt == nil])
            state.stillsPosted.append(id)
        }
        persist()
    }

    /// The scan package with every file field (clouds, mesh, keyframe files, each later segment's cloud) passed
    /// through `ref`: the gateway refuses a package with any local path left in it.
    static func withRefs(_ package: [String: Any], _ ref: @MainActor (Any?) async throws -> String?) async throws -> [String: Any] {
        var body = package
        for key in ["points_file", "mesh_file"] { body[key] = try await ref(body[key]) }
        if var kfs = body["keyframes"] as? [[String: Any]] {
            for i in kfs.indices {
                for key in ["image_file", "depth_file", "confidence_file"] { kfs[i][key] = try await ref(kfs[i][key]) }
            }
            body["keyframes"] = kfs
        }
        if var segs = body["segments"] as? [[String: Any]] {
            for i in segs.indices { segs[i]["points_file"] = try await ref(segs[i]["points_file"]) }
            body["segments"] = segs
        }
        return body
    }

    /// Size and modification time of a file (what `ref` compares to tell a rewritten file).
    static func stamp(of url: URL) -> String {
        let a = try? FileManager.default.attributesOfItem(atPath: url.path)
        let size = (a?[.size] as? Int) ?? -1
        let modified = (a?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        return "\(size)@\(modified)"
    }

    /// Uploads the file at a relative path once and returns its blob ref (nil stays nil). A file changed since its
    /// ref was made is uploaded again.
    private func ref(_ value: Any?) async throws -> String? {
        guard let path = value as? String else { return nil }
        if path.hasPrefix("blob://") { return path }
        let url = storage.dir(record).appending(path: path)
        let stamp = Self.stamp(of: url)
        if let done = state.cachedRef(path, stamp: stamp) { return done }
        let sha = try await Task.detached { try GatewayClient.sha256(of: url) }.value
        let type = path.hasSuffix(".jpg") ? "image/jpeg" : path.hasSuffix(".png") ? "image/png" : "application/octet-stream"
        let blob = try await gateway.uploadBlob(file: url, sha256: sha, contentType: type)
        state.refs[path] = blob
        var stamps = state.stamps ?? [:]
        stamps[path] = stamp
        state.stamps = stamps
        uploadedFiles += 1
        bytesSent += Int64((try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0)
        persist()
        return blob
    }

    private func loadJSON(_ url: URL) throws -> [String: Any] {
        guard let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any] else {
            throw GatewayError.message("\(url.lastPathComponent) is not a JSON object.")
        }
        return obj
    }

    private func persist() {
        guard !cancelled else { return }
        try? JSON.encoder.encode(state).write(to: storage.dir(record).appending(path: "upload.json"), options: .atomic)
    }
}
