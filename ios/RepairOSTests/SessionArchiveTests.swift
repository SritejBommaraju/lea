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
        // CI sets this so a real unzip tool (Python's zipfile) checks the archive after the tests.
        if let copy = ProcessInfo.processInfo.environment["SESSION_ZIP_COPY"] {
            try? fm.removeItem(atPath: copy)
            try fm.copyItem(at: zip, to: URL(fileURLWithPath: copy))
        }
    }

    /// A session that does not fit the 32-bit zip format throws instead of trapping. The limit is lowered so no test
    /// writes 4 GiB: a 200-byte file against limits that fit and that do not, including the last-file/final-offset case.
    @Test func throwsWhenTheArchiveWouldPassTheFormatLimit() throws {
        let fm = FileManager.default
        let tmp = fm.temporaryDirectory.appending(path: "archive-limit-\(UUID().uuidString)")
        let dir = tmp.appending(path: "s", directoryHint: .isDirectory)
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data(repeating: 7, count: 200).write(to: dir.appending(path: "a.bin"))
        try Data(repeating: 7, count: 200).write(to: dir.appending(path: "b.bin"))
        defer { try? fm.removeItem(at: tmp) }
        // Two entries: 2 x (30 + 5 + 200) local + 2 x (46 + 5) central + 22 end = 594 bytes.
        _ = try SessionArchive.zip(dir, into: tmp, limit: 594)
        #expect(throws: SessionArchive.Failure.self) { try SessionArchive.zip(dir, into: tmp, limit: 593) }   // only the end record overflows
        #expect(throws: SessionArchive.Failure.self) { try SessionArchive.zip(dir, into: tmp, limit: 300) }   // the second file overflows
        #expect(throws: SessionArchive.Failure.self) { try SessionArchive.zip(dir, into: tmp, limit: 100) }   // the first does
    }
}
