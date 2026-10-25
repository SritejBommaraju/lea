import Foundation
import Testing
@testable import RepairOS

@MainActor
struct StorageTests {
    func storage() -> ScanStorage {
        ScanStorage(root: FileManager.default.temporaryDirectory.appending(path: "repairos-tests-\(UUID().uuidString)"))
    }

    @Test func deleteRemovesTheFolder() throws {
        let s = storage()
        let a = try s.newSession(symptom: nil, engine: .arkit)
        let b = try s.newSession(symptom: "x", engine: .arkit)
        try Data(repeating: 1, count: 10_000).write(to: s.scanDir(a).appending(path: "f.bin"))
        #expect(s.listSessions().count == 2)
        #expect(s.usedBytes() >= 10_000)
        s.delete(a)
        #expect(s.listSessions().map(\.id) == [b.id])
        #expect(!FileManager.default.fileExists(atPath: s.dir(a).path))
    }

    @Test func refusesTheSessionBeingCaptured() throws {
        let s = storage()
        let a = try s.newSession(symptom: nil, engine: .arkit)
        let b = try s.newSession(symptom: nil, engine: .arkit)
        CaptureRegistry.active = a.id
        defer { CaptureRegistry.active = nil }
        #expect(!s.canDelete(a))
        s.delete(a)
        #expect(FileManager.default.fileExists(atPath: s.dir(a).path))
        #expect(s.deleteAll() == 1)
        #expect(s.listSessions().map(\.id) == [a.id])
        _ = b
    }

    @Test func deleteCancelsItsUploads() throws {
        let s = storage()
        let a = try s.newSession(symptom: nil, engine: .arkit)
        // A queue pointed at a closed port: cancelling must leave nothing behind and send nothing.
        let q = UploadQueue(record: a, storage: s, gateway: GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil))
        q.enqueueAll(stillIDs: ["x"], scanReady: true)
        s.delete(a)
        #expect(q.isIdle)
        #expect(!FileManager.default.fileExists(atPath: s.dir(a).path))
    }
}
