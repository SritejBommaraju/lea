import Foundation

/// One session folder as one zip, to hand to the design engine's import (it reads `scans/<scan_id>/scan_package.json`
/// relative to the session root). Only files already in the folder go in; nothing is generated.
enum SessionArchive {
    enum Failure: Error { case missingFolder, tooBig }

    /// Writes every file under `dir` into `<dir name>.zip` in `into`, with paths relative to `dir` (so the root entries
    /// are `app_session.json`, `scans/...`, `stills/...`). Entries are stored, not compressed: the PLY, JPEG and PNG
    /// files do not shrink much, and a stored zip needs no library. Foundation's own directory zipper
    /// (`NSFileCoordinator` `.forUploading`) was tried first and always wraps everything in a top folder named after the
    /// directory, which the importer does not expect. Blocking: call it off the main actor.
    static func zip(_ dir: URL, into: URL = FileManager.default.temporaryDirectory) throws -> URL {
        let fm = FileManager.default
        guard fm.fileExists(atPath: dir.path) else { throw Failure.missingFolder }
        let out = into.appending(path: "\(dir.lastPathComponent).zip")
        try? fm.removeItem(at: out)
        fm.createFile(atPath: out.path, contents: nil)
        let handle = try FileHandle(forWritingTo: out)
        defer { try? handle.close() }

        let root = dir.resolvingSymlinksInPath().path + "/"
        var files: [(name: String, url: URL)] = []
        if let walker = fm.enumerator(at: dir.resolvingSymlinksInPath(), includingPropertiesForKeys: [.isRegularFileKey]) {
            for case let url as URL in walker where (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true {
                files.append((String(url.resolvingSymlinksInPath().path.dropFirst(root.count)), url))
            }
        }
        files.sort { $0.name < $1.name }
        guard files.count < 0xFFFF else { throw Failure.tooBig }

        var central = Data()
        var offset = 0
        for (name, url) in files {
            let data = try Data(contentsOf: url, options: .mappedIfSafe)
            guard data.count < 0xFFFF_FFFF, offset < 0xFFFF_FFFF else { throw Failure.tooBig }
            let nameBytes = Data(name.utf8)
            let crc = crc32(data)
            let (time, date) = dosTime((try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? Date())
            var local = Data()
            local.le32(0x0403_4B50); local.le16(20); local.le16(0x0800); local.le16(0); local.le16(time); local.le16(date)
            local.le32(crc); local.le32(UInt32(data.count)); local.le32(UInt32(data.count))
            local.le16(UInt16(nameBytes.count)); local.le16(0); local.append(nameBytes)
            try handle.write(contentsOf: local)
            try handle.write(contentsOf: data)
            central.le32(0x0201_4B50); central.le16(20); central.le16(20); central.le16(0x0800); central.le16(0)
            central.le16(time); central.le16(date); central.le32(crc)
            central.le32(UInt32(data.count)); central.le32(UInt32(data.count))
            central.le16(UInt16(nameBytes.count)); central.le16(0); central.le16(0); central.le16(0); central.le16(0)
            central.le32(0); central.le32(UInt32(offset)); central.append(nameBytes)
            offset += local.count + data.count
        }
        var end = Data()
        end.le32(0x0605_4B50); end.le16(0); end.le16(0); end.le16(UInt16(files.count)); end.le16(UInt16(files.count))
        end.le32(UInt32(central.count)); end.le32(UInt32(offset)); end.le16(0)
        try handle.write(contentsOf: central)
        try handle.write(contentsOf: end)
        return out
    }

    private static let crcTable: [UInt32] = (0 ..< 256).map { n in
        (0 ..< 8).reduce(UInt32(n)) { c, _ in c & 1 == 1 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
    }

    static func crc32(_ data: Data) -> UInt32 {
        ~data.reduce(~UInt32(0)) { crcTable[Int(($0 ^ UInt32($1)) & 0xFF)] ^ ($0 >> 8) }
    }

    private static func dosTime(_ d: Date) -> (UInt16, UInt16) {
        let c = Calendar(identifier: .gregorian).dateComponents(in: .current, from: d)
        let year = max((c.year ?? 1980) - 1980, 0)
        return (UInt16(((c.hour ?? 0) << 11) | ((c.minute ?? 0) << 5) | ((c.second ?? 0) / 2)),
                UInt16((year << 9) | ((c.month ?? 1) << 5) | (c.day ?? 1)))
    }

    /// The entry names of a zip, read from its local file headers (PK 03 04: name length at offset 26, extra length at
    /// 28, compressed size at 18; an entry with a data descriptor is found by scanning to the next header).
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

private extension Data {
    mutating func le16(_ v: UInt16) { append(contentsOf: [UInt8(v & 0xFF), UInt8(v >> 8)]) }
    mutating func le32(_ v: UInt32) { le16(UInt16(v & 0xFFFF)); le16(UInt16(v >> 16)) }
}
