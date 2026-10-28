import Foundation

/// One session folder as one zip, to hand to the design engine's import (it reads `scans/<scan_id>/scan_package.json`
/// relative to the session root). Only files already in the folder go in; nothing is generated.
enum SessionArchive {
    enum Failure: Error { case missingFolder, noArchive }

    /// Zips `dir` with Foundation's own directory zipper and copies the result to `<dir name>.zip` in `into`, because
    /// the coordinator deletes its temporary zip when the block returns. Blocking: call it off the main actor.
    static func zip(_ dir: URL, into: URL = FileManager.default.temporaryDirectory) throws -> URL {
        guard FileManager.default.fileExists(atPath: dir.path) else { throw Failure.missingFolder }
        let out = into.appending(path: "\(dir.lastPathComponent).zip")
        try? FileManager.default.removeItem(at: out)
        var coordinationError: NSError?
        var copyError: Error?
        var copied = false
        NSFileCoordinator().coordinate(readingItemAt: dir, options: .forUploading, error: &coordinationError) { zipped in
            do {
                try FileManager.default.copyItem(at: zipped, to: out)
                copied = true
            } catch { copyError = error }
        }
        if let e = coordinationError ?? copyError { throw e }
        guard copied else { throw Failure.noArchive }
        return out
    }

    /// The entry names of a zip, read from its local file headers (PK 03 04: name length at offset 26, extra length at
    /// 28, compressed size at 18; entries with a data descriptor have size 0 there and are found by the next header).
    static func entryNames(_ zip: Data) -> [String] {
        func u16(_ i: Int) -> Int { Int(zip[i]) | Int(zip[i + 1]) << 8 }
        func u32(_ i: Int) -> Int { u16(i) | u16(i + 2) << 16 }
        var names: [String] = []
        var i = 0
        while i + 30 <= zip.count, u32(i) == 0x0403_4B50 {
            let nameLen = u16(i + 26), extraLen = u16(i + 28)
            let start = i + 30
            guard start + nameLen <= zip.count else { break }
            names.append(String(decoding: zip[start ..< start + nameLen], as: UTF8.self))
            var next = start + nameLen + extraLen + u32(i + 18)
            if u16(i + 6) & 8 != 0 {
                // Sizes follow the data: scan to the next local header.
                var j = start + nameLen + extraLen
                while j + 4 <= zip.count, u32(j) != 0x0403_4B50, u32(j) != 0x0201_4B50 { j += 1 }
                next = j
            }
            i = next
        }
        return names
    }
}
