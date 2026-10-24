import CryptoKit
import Combine
import Foundation
import ImageIO
import UniformTypeIdentifiers

/// What a photo is evidence of. Orbit photos feed reconstruction; close-ups are compared with a
/// saved reference photo; observations document a check or repair result.
enum PhotoRole: String, Codable, CaseIterable, Identifiable {
    case orbit, closeup, reference, observation
    var id: String { rawValue }
    var title: String {
        switch self {
        case .orbit: "Orbit"
        case .closeup: "Close-up"
        case .reference: "Reference"
        case .observation: "Result"
        }
    }
    var assetKind: String {
        switch self {
        case .orbit: "photo_orbit"
        case .closeup: "photo_closeup"
        case .reference: "reference_photo"
        case .observation: "photo_observation"
        }
    }
}

struct PhotoRecord: Codable, Identifiable {
    let id: UUID
    let filename: String
    let sha256: String
    let capturedAt: Date
    let width: Int
    let height: Int
    var role: PhotoRole?           // nil for photos captured before roles existed: orbit
    var subject: String?           // the area a close-up or reference photo shows

    var effectiveRole: PhotoRole { role ?? .orbit }
    var mediaType: String { UTType(filenameExtension: (filename as NSString).pathExtension)?.preferredMIMEType ?? "image/jpeg" }
}

struct VoiceClipRecord: Codable, Identifiable {
    let id: UUID
    let filename: String
    let sha256: String
    let recordedAt: Date
    let duration: TimeInterval
}

struct MeshReference: Codable, Identifiable {
    let id: UUID
    let filename: String
    let sha256: String
    let importedAt: Date
    var unitsPerMm: Double?        // 1 for a millimetre file, 0.001 for metres; declared at import
    var geometryFilename: String?  // an OBJ made from a USDZ original (Open3D can't read USDZ); the original is kept
    var geometrySha256: String?
}

/// A guided on-device scan: its stills are photos (role orbit) in the session; the model files sit beside them.
struct ScanRecord: Codable, Identifiable {
    let id: UUID
    let createdAt: Date
    let photoIds: [UUID]
    let usdzFilename: String?      // visual model (nil when building it failed)
    let usdzSha256: String?
    let objFilename: String?       // the same geometry for measurement
    let objSha256: String?
    let engine: String
}

struct CaptureSession: Codable, Identifiable {
    let id: UUID
    let createdAt: Date
    var photos: [PhotoRecord]
    var voiceClips: [VoiceClipRecord]
    var meshReferences: [MeshReference]
    var remoteSessionId: String?        // the bridge/SpacetimeDB session this local session syncs to
    var uploadedAssetIds: [String]?     // local record ids whose originals the bridge has verified
    var scans: [ScanRecord]?

    init(id: UUID = UUID(), createdAt: Date = Date()) {
        self.id = id
        self.createdAt = createdAt
        photos = []
        voiceClips = []
        meshReferences = []
    }
}

private struct CaptureIndex: Codable {
    var activeSessionID: UUID
    var sessions: [CaptureSession]
}

@MainActor
final class CaptureStore: ObservableObject {
    @Published private(set) var sessions: [CaptureSession] = []
    @Published private(set) var activeSessionID = UUID()
    @Published var errorMessage: String?

    private let rootURL: URL
    private let fileManager = FileManager.default

    var activeSession: CaptureSession {
        sessions.first(where: { $0.id == activeSessionID }) ?? CaptureSession(id: activeSessionID)
    }

    init() {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        rootURL = documents.appendingPathComponent("LeaCaptures", isDirectory: true)

        do {
            try fileManager.createDirectory(at: rootURL, withIntermediateDirectories: true)
            let indexURL = rootURL.appendingPathComponent("index.json")
            if fileManager.fileExists(atPath: indexURL.path) {
                let decoder = JSONDecoder()
                decoder.dateDecodingStrategy = .iso8601
                do {
                    let index = try decoder.decode(CaptureIndex.self, from: Data(contentsOf: indexURL))
                    sessions = index.sessions
                    activeSessionID = index.activeSessionID
                } catch {
                    // Never overwrite an index we can't read: set it aside (photos stay in their folders) and start fresh.
                    let aside = rootURL.appendingPathComponent("index.unreadable-\(Int(Date().timeIntervalSince1970)).json")
                    try fileManager.moveItem(at: indexURL, to: aside)
                    errorMessage = "Earlier sessions could not be read and were set aside as \(aside.lastPathComponent); their photos are still on this iPhone."
                }
            }
            if sessions.isEmpty || !sessions.contains(where: { $0.id == activeSessionID }) {
                let first = CaptureSession()
                sessions = [first]
                activeSessionID = first.id
                try persist()
            }
        } catch {
            let first = CaptureSession()
            sessions = [first]
            activeSessionID = first.id
            errorMessage = "Local capture storage could not be opened: \(error.localizedDescription)"
        }
    }

    func newSession() {
        let session = CaptureSession()
        sessions.insert(session, at: 0)
        activeSessionID = session.id
        saveIndex()
    }

    func selectSession(_ id: UUID) {
        guard sessions.contains(where: { $0.id == id }) else { return }
        activeSessionID = id
        saveIndex()
    }

    func photoURL(_ photo: PhotoRecord, in session: CaptureSession) -> URL {
        sessionURL(session.id).appendingPathComponent(photo.filename)
    }

    func meshURL(_ mesh: MeshReference, in session: CaptureSession) -> URL {
        sessionURL(session.id).appendingPathComponent(mesh.filename)
    }

    func addPhoto(_ data: Data, role: PhotoRole = .orbit, subject: String? = nil) {
        do {
            let id = UUID()
            let filename = "photo-\(id.uuidString).jpg"
            let folder = try ensureSessionFolder()
            try data.write(to: folder.appendingPathComponent(filename), options: .atomic)
            let (width, height) = imageDimensions(data)
            let record = PhotoRecord(
                id: id,
                filename: filename,
                sha256: hash(data),
                capturedAt: Date(),
                width: width,
                height: height,
                role: role,
                subject: subject
            )
            mutateActiveSession { $0.photos.append(record) }
        } catch {
            errorMessage = "Photo was not saved: \(error.localizedDescription)"
        }
    }

    func addVoiceClip(from temporaryURL: URL, duration: TimeInterval) {
        do {
            let data = try Data(contentsOf: temporaryURL)
            let id = UUID()
            let filename = "voice-\(id.uuidString).m4a"
            let folder = try ensureSessionFolder()
            try data.write(to: folder.appendingPathComponent(filename), options: .atomic)
            let record = VoiceClipRecord(
                id: id,
                filename: filename,
                sha256: hash(data),
                recordedAt: Date(),
                duration: duration
            )
            mutateActiveSession { $0.voiceClips.append(record) }
            try? fileManager.removeItem(at: temporaryURL)
        } catch {
            errorMessage = "Voice clip was not saved: \(error.localizedDescription)"
        }
    }

    func importMesh(from sourceURL: URL, unitsPerMm: Double = 1) {
        let allowed = Set(["obj", "stl", "ply", "usdz", "glb"])
        let fileExtension = sourceURL.pathExtension.lowercased()
        guard allowed.contains(fileExtension) else {
            errorMessage = "Choose an OBJ, STL, PLY, USDZ or GLB file."
            return
        }

        let accessGranted = sourceURL.startAccessingSecurityScopedResource()
        defer { if accessGranted { sourceURL.stopAccessingSecurityScopedResource() } }

        do {
            let data = try Data(contentsOf: sourceURL)
            let id = UUID()
            let filename = "reference-\(id.uuidString).\(fileExtension)"
            let folder = try ensureSessionFolder()
            try data.write(to: folder.appendingPathComponent(filename), options: .atomic)
            var record = MeshReference(id: id, filename: filename, sha256: hash(data), importedAt: Date(), unitsPerMm: unitsPerMm)
            if fileExtension == "usdz" {
                let objName = "reference-\(id.uuidString).obj"
                try ObjectScanner.exportOBJ(folder.appendingPathComponent(filename), to: folder.appendingPathComponent(objName))
                record.geometryFilename = objName
                record.geometrySha256 = hash(try Data(contentsOf: folder.appendingPathComponent(objName)))
            }
            mutateActiveSession { $0.meshReferences.append(record) }
        } catch {
            errorMessage = "Mesh was not imported: \(error.localizedDescription)"
        }
    }

    /// Moves a finished scan's stills and model into the active session. Files are moved, not copied, and hashed
    /// off the main thread (a scan can be a few hundred megabytes).
    func addScan(images: URL, usdz: URL?, obj: URL?, engine: String) async {
        do {
            let target = activeSessionID          // the session this scan belongs to, even if another is opened meanwhile
            let folder = try ensureSessionFolder()
            let scanId = UUID()
            let moved = try await Task.detached { () throws -> ([PhotoRecord], (String, String)?, (String, String)?) in
                let fm = FileManager.default
                let stills = try fm.contentsOfDirectory(at: images, includingPropertiesForKeys: [.creationDateKey])
                    .filter { ["heic", "jpg", "jpeg"].contains($0.pathExtension.lowercased()) }
                    .sorted { $0.lastPathComponent < $1.lastPathComponent }
                var photos: [PhotoRecord] = []
                for still in stills {
                    let id = UUID()
                    let name = "photo-\(id.uuidString).\(still.pathExtension.lowercased())"
                    let dest = folder.appendingPathComponent(name)
                    try fm.moveItem(at: still, to: dest)
                    let digest = try Self.fileHash(dest)
                    let size = Self.imageDimensions(at: dest)
                    let date = (try? still.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
                    photos.append(PhotoRecord(id: id, filename: name, sha256: digest, capturedAt: date, width: size.0, height: size.1, role: .orbit, subject: nil))
                }
                func place(_ url: URL?, _ ext: String) throws -> (String, String)? {
                    guard let url, fm.fileExists(atPath: url.path) else { return nil }
                    let name = "scan-\(scanId.uuidString).\(ext)"
                    try fm.moveItem(at: url, to: folder.appendingPathComponent(name))
                    return (name, try Self.fileHash(folder.appendingPathComponent(name)))
                }
                return (photos, try place(usdz, "usdz"), try place(obj, "obj"))
            }.value
            let scan = ScanRecord(id: scanId, createdAt: Date(), photoIds: moved.0.map(\.id), usdzFilename: moved.1?.0, usdzSha256: moved.1?.1,
                                  objFilename: moved.2?.0, objSha256: moved.2?.1, engine: engine)
            mutate(target) { session in
                session.photos.append(contentsOf: moved.0)
                session.scans = (session.scans ?? []) + [scan]
            }
            try? fileManager.removeItem(at: images.deletingLastPathComponent())
        } catch {
            errorMessage = "The scan was not saved: \(error.localizedDescription)"
        }
    }

    nonisolated private static func fileHash(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    nonisolated private static func imageDimensions(at url: URL) -> (Int, Int) {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else { return (0, 0) }
        return (p[kCGImagePropertyPixelWidth] as? Int ?? 0, p[kCGImagePropertyPixelHeight] as? Int ?? 0)
    }

    // MARK: Bridge sync bookkeeping

    func sessionURL(for session: CaptureSession) -> URL { sessionURL(session.id) }

    func setRemoteSessionId(_ remoteId: String, for localId: UUID) {
        mutate(localId) { $0.remoteSessionId = remoteId }
    }

    func setUploaded(_ keys: [String], in localSessionId: UUID) {
        mutate(localSessionId) { $0.uploadedAssetIds = keys }
    }

    func markUploaded(_ key: String, in localSessionId: UUID) {
        mutate(localSessionId) { session in
            var ids = session.uploadedAssetIds ?? []
            if !ids.contains(key) { ids.append(key) }
            session.uploadedAssetIds = ids
        }
    }

    private func mutate(_ id: UUID, _ change: (inout CaptureSession) -> Void) {
        guard let index = sessions.firstIndex(where: { $0.id == id }) else { return }
        change(&sessions[index])
        saveIndex()
    }

    private func mutateActiveSession(_ change: (inout CaptureSession) -> Void) {
        guard let index = sessions.firstIndex(where: { $0.id == activeSessionID }) else { return }
        change(&sessions[index])
        saveIndex()
    }

    private func saveIndex() {
        do { try persist() }
        catch { errorMessage = "Capture index was not saved: \(error.localizedDescription)" }
    }

    private func persist() throws {
        let index = CaptureIndex(activeSessionID: activeSessionID, sessions: sessions)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(index)
        try data.write(to: rootURL.appendingPathComponent("index.json"), options: .atomic)
    }

    private func ensureSessionFolder() throws -> URL {
        let folder = sessionURL(activeSessionID)
        try fileManager.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    private func sessionURL(_ id: UUID) -> URL {
        rootURL.appendingPathComponent(id.uuidString, isDirectory: true)
    }

    private func hash(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func imageDimensions(_ data: Data) -> (Int, Int) {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int else {
            return (0, 0)
        }
        return (width, height)
    }
}
