import Foundation
import Observation
import SwiftUI
import UIKit

/// Talking with Lea before the scan. The conversation gets a gateway session and a scan id at once, and the scan that
/// follows uses the same session and scan id, so the conversation simply carries on at its report. (The label photo
/// and the product brief the gateway used to research were removed on 2026-10-28: the product is named by its design
/// id, Settings > Product.)
@MainActor
@Observable
final class PreScan {
    static let shared = PreScan()

    private(set) var sessionID: String?
    private(set) var scanID = PreScan.newScanID()
    private(set) var live: LiveEvents?
    @ObservationIgnored private var starting: Task<Void, Never>?

    static func newScanID() -> String { "scan_" + ScanStorage.hex(8) }

    /// The gateway session for this conversation (made once).
    func begin(gateway: GatewayClient) async {
        if sessionID != nil { return }
        if let starting { await starting.value; return }
        let t = Task { [weak self] in
            guard let self else { return }
            if let id = try? await gateway.createSession(symptom: nil) {
                sessionID = id
                let events = LiveEvents(scanID: scanID, diag: WakeListener.diag)
                events.start(gateway: gateway) { [weak self] in self?.sessionID }
                live = events
                WakeListener.diag.log("prescan_session", ["ok": true])
            } else {
                WakeListener.diag.log("prescan_session", ["ok": false])
            }
        }
        starting = t
        await t.value
        starting = nil
    }

    /// The scan that starts now uses this conversation's session and scan id (nil when there was no conversation).
    func takeRecord(storage: ScanStorage, symptom: String?, engine: EngineKind) throws -> SessionRecord? {
        guard let sid = sessionID else { return nil }
        let stamp = ISO8601DateFormatter.compact.string(from: Date())
        var record = SessionRecord(id: "sess_local_\(stamp)_\(ScanStorage.hex(4))", createdAt: Date(),
                                   symptom: symptom?.trimmedOrNil, productID: "prod_\(ScanStorage.hex(8))",
                                   scanID: scanID, engine: engine)
        record.gatewaySessionID = sid
        try FileManager.default.createDirectory(at: storage.scanDir(record), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: storage.stillsDir(record), withIntermediateDirectories: true)
        try storage.save(record)
        // The upload reuses the session the conversation made instead of creating another.
        var state = UploadQueue.State()
        state.gatewaySessionID = sid
        try JSON.encoder.encode(state).write(to: storage.dir(record).appending(path: "upload.json"), options: .atomic)
        WakeListener.diag.log("prescan_scan", [:])
        live?.stop()
        live = nil
        sessionID = nil
        scanID = Self.newScanID()
        return record
    }
}
