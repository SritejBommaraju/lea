import Foundation

/// Uploads several files at once (`width` at a time; over the internet each file costs a round trip, so one at a time
/// leaves the link idle), each path once even when asked for twice at once, and stops cleanly: `stop()` cancels the
/// uploads under way and refuses new ones (a deleted session's files must not keep the connection busy).
@MainActor
final class UploadBatcher {
    let width: Int
    private var running: [String: Task<String?, Error>] = [:]
    private(set) var stopped = false

    init(width: Int) { self.width = max(1, width) }

    /// The upload of `path`, shared with a caller already waiting on the same path. A failed upload is not kept:
    /// asking again runs it again.
    func one(_ path: String, _ work: @escaping @MainActor () async throws -> String?) async throws -> String? {
        if stopped { throw CancellationError() }
        if let task = running[path] { return try await task.value }
        let task = Task { try await work() }
        running[path] = task
        defer { if running[path] == task { running[path] = nil } }
        return try await task.value
    }

    /// Every path, `width` at a time; the first failure ends the batch (the paths not started are not started).
    func all(_ paths: [String], _ work: @escaping @MainActor (String) async throws -> String?) async throws {
        var rest = paths[...]
        while !rest.isEmpty {
            if stopped { throw CancellationError() }
            let batch = rest.prefix(width)
            rest = rest.dropFirst(batch.count)
            try await withThrowingTaskGroup(of: Void.self) { group in
                for path in batch { group.addTask { _ = try await self.one(path) { try await work(path) } } }
                try await group.waitForAll()
            }
        }
    }

    func stop() {
        stopped = true
        running.values.forEach { $0.cancel() }
        running = [:]
    }
}
