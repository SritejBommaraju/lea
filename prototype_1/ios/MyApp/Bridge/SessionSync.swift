import Combine
import Foundation
import UIKit

/// Mirrors the active local session to the bridge and keeps its live state (from SpacetimeDB via the
/// bridge's event stream) on screen. Local originals are never modified or deleted by syncing.
@MainActor
final class SessionSync: ObservableObject {
    @Published private(set) var health: BridgeHealth?
    @Published private(set) var snapshot: SessionSnapshot?
    @Published private(set) var grain: GrainLive?
    @Published private(set) var isSyncing = false
    @Published private(set) var pendingUploads = 0
    @Published private(set) var connection: String = "Not paired"
    @Published var message: String?
    @Published private(set) var queued = 0      // requests saved while the bridge was unreachable

    private(set) var client: BridgeClient?
    private var liveTask: Task<Void, Never>?
    private var grainTask: Task<Void, Never>?
    private weak var store: CaptureStore?

    func attach(_ store: CaptureStore) {
        self.store = store
        reconnect()
    }

    func reconnect() {
        client = BridgeSettings.client()
        liveTask?.cancel()
        guard let client else { connection = "Not paired"; return }
        connection = "Connecting…"
        Task {
            do {
                health = try await client.health()
                connection = health?.spacetimedb.connected == true ? "Connected" : "Bridge up, SpacetimeDB down"
                await syncActiveSession()
            } catch {
                connection = "Offline"
                message = error.localizedDescription
            }
        }
    }

    var remoteId: String? { store?.activeSession.remoteSessionId }

    /// Creates the remote session if needed, then uploads every original not yet verified by the bridge.
    func syncActiveSession() async {
        guard let client, let store, !isSyncing else { return }
        isSyncing = true
        // Ask iOS for time to finish uploads in flight if the app is sent to the background.
        let background = UIApplication.shared.beginBackgroundTask(withName: "Lea upload")
        defer {
            isSyncing = false
            UIApplication.shared.endBackgroundTask(background)
        }
        await replayOutbox()
        let local = store.activeSession
        do {
            let remote = local.remoteSessionId ?? "se_\(local.id.uuidString.replacingOccurrences(of: "-", with: "").lowercased())"
            // The bridge's own asset list decides what is uploaded, so a reset or different bridge gets everything again.
            let existing: SessionSnapshot
            do {
                existing = try await client.snapshot(remote)
            } catch BridgeClient.Failure.http(404, _) {
                existing = try await client.createSession(id: remote, title: "Repair \(local.createdAt.formatted(date: .abbreviated, time: .shortened))")
            }
            store.setRemoteSessionId(remote, for: local.id)
            let onBridge = Set(existing.assets.map(\.id))
            var remoteIds: [String: String] = [:]   // local key -> bridge asset id
            for id in local.photos.map(\.id) + local.meshReferences.map(\.id) + local.voiceClips.map(\.id) { remoteIds[id.uuidString] = Self.assetId(id) }
            for scan in local.scans ?? [] { for key in ["obj-\(scan.id.uuidString)", "usdz-\(scan.id.uuidString)"] { remoteIds[key] = Self.scanAssetId(key) } }
            let uploaded = Set(remoteIds.filter { onBridge.contains($0.value) }.keys)
            store.setUploaded(Array(uploaded), in: local.id)
            let folder = store.sessionURL(for: local)
            var work: [(UUID, String, URL, String, String, [String: String])] = []
            for p in local.photos where !uploaded.contains(p.id.uuidString) {
                var meta = ["capturedAt": ISO8601DateFormatter().string(from: p.capturedAt), "width": "\(p.width)", "height": "\(p.height)"]
                if let subject = p.subject { meta["subject"] = subject }
                work.append((p.id, p.effectiveRole.assetKind, folder.appendingPathComponent(p.filename), p.mediaType, p.sha256, meta))
            }
            for m in local.meshReferences where !uploaded.contains(m.id.uuidString) {
                // A USDZ original is measured through its OBJ copy; the original stays on this iPhone byte-for-byte.
                let file = m.geometryFilename ?? m.filename
                var meta = ["extension": (file as NSString).pathExtension.lowercased(), "unitsPerMm": "\(m.unitsPerMm ?? 1)",
                            "importedAt": ISO8601DateFormatter().string(from: m.importedAt)]
                if m.geometryFilename != nil { meta["convertedFrom"] = "usdz"; meta["originalSha256"] = m.sha256 }
                work.append((m.id, "reference_mesh", folder.appendingPathComponent(file), "application/octet-stream", m.geometrySha256 ?? m.sha256, meta))
            }
            var scanWork: [(String, String, URL, String, String, [String], [String: String])] = []
            for scan in local.scans ?? [] {
                let sources = scan.photoIds.map(Self.assetId)
                if let obj = scan.objFilename, let sha = scan.objSha256, !uploaded.contains("obj-\(scan.id.uuidString)") {
                    scanWork.append(("obj-\(scan.id.uuidString)", "recon_mesh", folder.appendingPathComponent(obj), "model/obj", sha, sources, ["engine": scan.engine, "extension": "obj"]))
                }
                if let usdz = scan.usdzFilename, let sha = scan.usdzSha256, !uploaded.contains("usdz-\(scan.id.uuidString)") {
                    scanWork.append(("usdz-\(scan.id.uuidString)", "recon_bundle", folder.appendingPathComponent(usdz), "model/vnd.usdz+zip", sha, sources, ["engine": scan.engine, "purpose": "visual model"]))
                }
            }
            for v in local.voiceClips where !uploaded.contains(v.id.uuidString) {
                work.append((v.id, "voice", folder.appendingPathComponent(v.filename), "audio/mp4", v.sha256, ["duration": "\(v.duration)"]))
            }
            pendingUploads = work.count + scanWork.count
            for (id, kind, url, type, digest, meta) in work {
                _ = try await client.uploadAsset(sessionId: remote, assetId: Self.assetId(id), kind: kind, fileURL: url, mediaType: type, sha256: digest, meta: meta)
                store.markUploaded(id.uuidString, in: local.id)
                pendingUploads -= 1
            }
            // Scan models after their photos: the bridge requires a derived file's sources to exist first.
            for (key, kind, url, type, digest, sources, meta) in scanWork {
                _ = try await client.uploadAsset(sessionId: remote, assetId: Self.scanAssetId(key), kind: kind, fileURL: url, mediaType: type,
                                                 sha256: digest, sourceAssetIds: sources, meta: meta)
                store.markUploaded(key, in: local.id)
                pendingUploads -= 1
            }
            var snap = try await client.snapshot(remote)
            if snap.session.referenceMeshAssetId.isEmpty, let first = local.meshReferences.first {
                snap = try await client.pinReference(sessionId: remote, assetId: Self.assetId(first.id))
            }
            snapshot = snap
            startLive(remote, after: snap.lastSeq)
        } catch {
            message = error.localizedDescription
        }
    }

    static func assetId(_ localId: UUID) -> String { "as_\(localId.uuidString.replacingOccurrences(of: "-", with: "").lowercased())" }
    /// "obj-<scan uuid>" / "usdz-<scan uuid>" -> a bridge asset id.
    static func scanAssetId(_ key: String) -> String { "as_\(key.replacingOccurrences(of: "-", with: "").lowercased())" }

    private func startLive(_ remote: String, after: Int) {
        liveTask?.cancel()
        guard let client else { return }
        liveTask = Task { [weak self] in
            var last = after
            while !Task.isCancelled {
                do {
                    for try await event in client.events(sessionId: remote, after: last) {
                        last = event.seq
                        self?.snapshot = try await client.snapshot(remote)
                    }
                } catch { self?.connection = "Reconnecting…" }
                try? await Task.sleep(for: .seconds(2))
                if let snap = try? await client.snapshot(remote) { self?.snapshot = snap; self?.connection = "Connected" }
            }
        }
    }

    /// Polls Grain's live state through the bridge while the Design screen is visible.
    func watchGrain(_ on: Bool) {
        grainTask?.cancel()
        guard on, let client, let remote = remoteId else { return }
        grainTask = Task { [weak self] in
            while !Task.isCancelled {
                if let live = try? await client.grainLive(sessionId: remote) { self?.grain = live }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    // MARK: Offline outbox

    struct Saved: Codable { let method: String; let path: String; let body: Data }
    private var outbox: [Saved] {
        get { (try? JSONDecoder().decode([Saved].self, from: Data(contentsOf: Self.outboxURL))) ?? [] }
        set { try? JSONEncoder().encode(newValue).write(to: Self.outboxURL, options: .atomic); queued = newValue.count }
    }
    private static let outboxURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("lea-outbox.json")

    /// For requests that are safe to send twice (they carry their own id or are refused once done): send now, or
    /// keep for later if the bridge is unreachable. Returns false only when the bridge refused it.
    @discardableResult
    func sendOrKeep<B: Encodable>(_ method: String, _ path: (String) -> String, _ body: B) async -> Bool {
        guard let client, let remote = remoteId else { message = BridgeClient.Failure.notConfigured.errorDescription; return false }
        let saved = Saved(method: method, path: path(remote), body: BridgeClient.encodedBody(body))
        do {
            try await client.sendRaw(saved.method, saved.path, json: saved.body)
            snapshot = try? await client.snapshot(remote)
            return true
        } catch BridgeClient.Failure.transport {
            try? FileManager.default.createDirectory(at: Self.outboxURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            outbox.append(saved)
            message = "The bridge is unreachable; Lea will send this when it's back."
            return true
        } catch {
            message = error.localizedDescription
            return false
        }
    }

    private func replayOutbox() async {
        guard let client, !outbox.isEmpty else { queued = outbox.count; return }
        var left: [Saved] = []
        for saved in outbox {
            do { try await client.sendRaw(saved.method, saved.path, json: saved.body) }
            catch BridgeClient.Failure.transport { left.append(saved) }   // still offline: keep it
            catch { message = "A saved request was refused: \(error.localizedDescription)" }   // already done or no longer valid
        }
        outbox = left
    }

    /// Runs a bridge call, surfaces its error, refreshes the snapshot.
    func perform<T>(_ action: (BridgeClient, String) async throws -> T) async -> T? {
        guard let client, let remote = remoteId else { message = BridgeClient.Failure.notConfigured.errorDescription; return nil }
        do {
            let result = try await action(client, remote)
            snapshot = try await client.snapshot(remote)
            return result
        } catch {
            message = error.localizedDescription
            return nil
        }
    }
}
