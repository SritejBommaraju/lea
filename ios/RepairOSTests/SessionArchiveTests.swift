import Foundation
import Testing
@testable import RepairOS

struct SessionArchiveTests {
    /// The zip the share action hands out lists the session's files by paths relative to the session root, which is
    /// what the design engine's import reads (`scans/<scan_id>/scan_package.json`).
    @Test func zipListsRelativePathsFromTheSessionRoot() throws {
        let tmp = FileManager.default.temporaryDirectory.appending(path: "archive-test-\(UUID().uuidString)")
        let dir = tmp.appending(path: "session_x", directoryHint: .isDirectory)
        let fm = FileManager.default
        try fm.createDirectory(at: dir.appending(path: "scans/scan_x"), withIntermediateDirectories: true)
        try fm.createDirectory(at: dir.appending(path: "stills"), withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: dir.appending(path: "app_session.json"))
        try Data("{\"scan_id\":\"scan_x\"}".utf8).write(to: dir.appending(path: "scans/scan_x/scan_package.json"))
        try Data("{}".utf8).write(to: dir.appending(path: "stills/s1.json"))
        defer { try? fm.removeItem(at: tmp) }

        let zip = try SessionArchive.zip(dir, into: tmp)
        #expect(zip.lastPathComponent == "session_x.zip")
        let names = SessionArchive.entryNames(try Data(contentsOf: zip))
        #expect(names.contains("app_session.json"), "entries were \(names)")
        #expect(names.contains("scans/scan_x/scan_package.json"), "entries were \(names)")
        #expect(names.contains("stills/s1.json"), "entries were \(names)")
    }
}
