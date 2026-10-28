import Foundation

/// The person's answers to "Did this fix it?", kept on the phone until the gateway has them. This tap is also the
/// label the design engine learns from, so it is saved first (a file beside the session, `outcomes.json`), sent as
/// soon as the network allows, and never lost to a dropped connection or a closed app.
///
/// It is separate from UploadQueue on purpose: that queue moves a scan's files and gates `complete`, and an answer
/// given days later, with the report reopened offline, must not wait behind (or hold up) a capture.
struct OutcomeOutbox: Codable, Equatable, Sendable {
    struct Entry: Codable, Equatable, Sendable {
        var rowID: String
        /// What the row is about (`kind:ref`), which stays the same when the report is made again and the row gets a
        /// new id. Older files have none.
        var key: String?
        var choice: OutcomeChoice
        var note: String?
        var at: Date
        /// The gateway has it (it answered 200).
        var sent: Bool
    }

    /// One entry per report row: a newer answer replaces the older.
    var entries: [String: Entry] = [:]

    mutating func record(rowID: String, key: String? = nil, choice: OutcomeChoice, note: String? = nil, at: Date = Date()) {
        if let key { entries = entries.filter { $0.value.key != key || $0.key == rowID } }   // one answer per part
        entries[rowID] = Entry(rowID: rowID, key: key, choice: choice, note: note?.trimmedOrNil, at: at, sent: false)
    }

    /// The report was made again and this part's row has a new id: the saved answer moves to it (and is sent under it).
    /// Returns true when an answer moved.
    @discardableResult
    mutating func adopt(rowID: String, key: String) -> Bool {
        guard entries[rowID] == nil, let old = entries.values.first(where: { $0.key == key }) else { return false }
        entries[old.rowID] = nil
        var moved = old
        moved.rowID = rowID
        entries[rowID] = moved
        return true
    }

    /// Marks an answer sent, but only if it is still the one that was sent: the person may have changed it meanwhile.
    mutating func markSent(_ entry: Entry) {
        guard var current = entries[entry.rowID], current.at == entry.at, current.choice == entry.choice else { return }
        current.sent = true
        entries[entry.rowID] = current
    }

    var unsent: [Entry] { entries.values.filter { !$0.sent }.sorted { $0.at < $1.at } }
}

/// Where each answer stands, in words the card can show.
enum OutcomeStatus: Equatable, Sendable {
    /// The gateway has it.
    case saved
    /// Saved on the phone; goes when the phone is online.
    case queued
    case sending
    /// The gateway refused it, with its reason. The answer stays on the phone.
    case failed(String)

    var words: String {
        switch self {
        case .saved: "Saved. Thank you."
        case .queued: "Will send when you are online."
        case .sending: "Sending\u{2026}"
        case .failed(let why): "Could not send: \(why)"
        }
    }

    /// An offline error is a wait, not a failure; anything the gateway answered is its reason.
    static func after(_ error: Error) -> OutcomeStatus {
        if error is URLError { return .queued }
        return .failed((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
    }
}

/// The report's outcome answers: reads what the gateway already has, keeps the phone's own, and sends.
@MainActor
final class OutcomeBoard: ObservableObject {
    @Published private(set) var outbox = OutcomeOutbox()
    @Published private(set) var status: [String: OutcomeStatus] = [:]

    private var scanID = ""
    private var file: URL?
    private var gateway: GatewayClient?
    private var retry: Task<Void, Never>?
    private var flushing = false
    private var dirty = false

    /// Called once the report is on screen. `directory` is the session folder; nil keeps answers in memory only (a demo
    /// replay never sends anything).
    func configure(scanID: String, directory: URL?, gateway: GatewayClient?) {
        self.scanID = scanID
        self.gateway = gateway
        file = directory?.appending(path: "outcomes.json")
        if let file, let data = try? Data(contentsOf: file), let saved = try? JSON.decoder.decode(OutcomeOutbox.self, from: data) {
            outbox = saved
        }
        for entry in outbox.entries.values { status[entry.rowID] = entry.sent ? .saved : .queued }
        start()
    }

    /// The answer to show for a row: the phone's own (it may be newer than the report), else the gateway's.
    func answer(rowID: String, server: OutcomeAnswer?) -> OutcomeChoice? {
        outbox.entries[rowID]?.choice ?? server?.choice
    }

    /// A row on screen: if the phone holds an answer for the same part under an older row id (the report was made again
    /// since), the answer moves to this row, so it shows here and is sent to a row the gateway still has.
    func adopt(rowID: String, key: String) {
        guard outbox.adopt(rowID: rowID, key: key) else { return }
        status[rowID] = outbox.entries[rowID]?.sent == true ? .saved : .queued
        persist()
        Task { await flush() }
    }

    /// Records the answer and sends it. A changed answer replaces the earlier one.
    func set(_ choice: OutcomeChoice, rowID: String, key: String? = nil, note: String? = nil) {
        outbox.record(rowID: rowID, key: key, choice: choice, note: note)
        status[rowID] = .sending
        guard persist() else {
            // Not on the phone: never say it will be sent later.
            status[rowID] = .failed("it could not be saved on this phone (is the storage full?)")
            return
        }
        Task { await flush() }
    }

    /// Sends every answer the gateway does not have yet, oldest first. An offline phone keeps them queued. An answer
    /// given while a send is under way is picked up by the next pass of the same call.
    func flush() async {
        guard !flushing else { dirty = true; return }
        guard let gateway else {
            for e in outbox.unsent { status[e.rowID] = .queued }
            return
        }
        flushing = true
        defer { flushing = false }
        repeat {
            dirty = false
            var offline = false
            for entry in outbox.unsent {
                if offline { status[entry.rowID] = .queued; continue }
                status[entry.rowID] = .sending
                do {
                    try await gateway.postOutcome(scanID: scanID, rowID: entry.rowID, outcome: entry.choice, note: entry.note)
                    outbox.markSent(entry)
                    persist()
                    status[entry.rowID] = outbox.entries[entry.rowID]?.sent == true ? .saved : .sending
                } catch {
                    let s = OutcomeStatus.after(error)
                    status[entry.rowID] = s
                    if s == .queued { offline = true }   // the rest would fail the same way
                }
            }
        } while dirty
    }

    /// Looks again every 20 seconds, so an answer given offline goes when the phone is back.
    private func start() {
        retry?.cancel()
        retry = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.flush()
                try? await Task.sleep(for: .seconds(20))
            }
        }
    }

    func stop() { retry?.cancel() }

    /// Writes the outbox beside the session. false when it could not be written; true also when there is no folder
    /// (a demo replay keeps answers in memory only).
    @discardableResult
    private func persist() -> Bool {
        guard let file else { return true }
        do {
            try JSON.encoder.encode(outbox).write(to: file, options: .atomic)
            return true
        } catch {
            return false
        }
    }
}
