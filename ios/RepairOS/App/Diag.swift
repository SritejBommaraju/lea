import Foundation
import os

/// Per-session diagnostics: one JSON line per event in `<session>/diag.jsonl` (pull it with `devicectl device
/// copy from --domain-type appDataContainer`), mirrored to os.Logger. Every line carries free memory and the
/// thermal state, because kills under memory or heat pressure leave no crash log.
final class Diag: @unchecked Sendable {
    private let url: URL?
    private let queue = DispatchQueue(label: "diag")
    private let logger = Logger(subsystem: "com.repairos.app", category: "diag")
    private let start = Date()

    private init(url: URL?) {
        self.url = url
    }

    nonisolated(unsafe) private static var shared: [URL: Diag] = [:]
    private static let lock = NSLock()

    /// The session's log; every component of a session shares one so lines never interleave.
    static func session(_ sessionDir: URL) -> Diag {
        let url = sessionDir.appending(path: "diag.jsonl")
        lock.lock()
        defer { lock.unlock() }
        if let d = shared[url] { return d }
        let d = Diag(url: url)
        shared[url] = d
        return d
    }

    func log(_ event: String, _ fields: [String: Any] = [:]) {
        var line = fields
        line["event"] = event
        line["t"] = ISO8601DateFormatter().string(from: Date())
        line["ms"] = Int(Date().timeIntervalSince(start) * 1000)
        line["mem_mb"] = os_proc_available_memory() >> 20
        line["thermal"] = Self.thermal
        let text = (try? JSONSerialization.data(withJSONObject: line.mapValues(Self.plain), options: [.sortedKeys]))
            .map { String(decoding: $0, as: UTF8.self) } ?? "{\"event\":\"\(event)\"}"
        logger.info("\(text, privacy: .public)")
        guard let url else { return }
        queue.async {
            let data = Data((text + "\n").utf8)
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile()
                h.write(data)
                try? h.close()
            } else {
                try? data.write(to: url)
            }
        }
    }

    static var thermal: String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: "nominal"
        case .fair: "fair"
        case .serious: "serious"
        case .critical: "critical"
        @unknown default: "unknown"
        }
    }

    private static func plain(_ v: Any) -> Any {
        switch v {
        case let s as String: s
        case let n as NSNumber: n
        case let a as [Any]: a.map(plain)
        case let d as [String: Any]: d.mapValues(plain)
        default: String(describing: v)
        }
    }
}
