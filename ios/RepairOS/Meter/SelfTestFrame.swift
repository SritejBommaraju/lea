import Foundation

/// What the product sends back over Bluetooth when it is asked to check itself, as bytes. Pure functions only: no
/// radio here, so every line is unit-tested (`RepairOSTests/SelfTestTests.swift`); `MeterLink` does the talking.
///
/// The wire format is the product's own, written by the design engine into the firmware:
/// `u8 version (1), u8 count`, then per check `u8 index, u8 kind (1 rail, 2 output), i32 little-endian value
/// (millivolts for a rail, 0 or 1 for an output), u8 ok (0 fail, 1 ok, 2 unknown)`. Which net an index is, and what
/// a good value is, comes from the report's `self_test.checks`, never from this file.
struct SelfTestFrame: Equatable, Sendable {
    enum Kind: UInt8, Sendable {
        case rail = 1, output = 2
    }

    /// The product's own verdict on one check.
    enum Outcome: UInt8, Sendable {
        case fail = 0, ok = 1, unknown = 2

        var contract: Ok {
            switch self {
            case .fail: .fail
            case .ok: .ok
            case .unknown: .unknown
            }
        }
    }

    struct Item: Equatable, Sendable {
        var index: Int
        var kind: Kind
        /// Millivolts for a rail, 0 or 1 for an output.
        var value: Int32
        var outcome: Outcome
    }

    /// Why bytes are not a frame. Nothing is guessed: an unknown kind or outcome refuses the whole frame.
    enum DecodeError: Error, Equatable, Sendable {
        case tooShort
        case unsupportedVersion(Int)
        /// The header promises more checks than the bytes hold (a notification cut at the connection's size: read again).
        case truncated(needed: Int, got: Int)
        case unknownKind(UInt8)
        case unknownOutcome(UInt8)
        case outputNotZeroOrOne(Int32)
    }

    static let supportedVersion = 1
    /// Bytes per check.
    static let itemSize = 7

    var items: [Item]

    static func decode(_ data: Data) -> Result<SelfTestFrame, DecodeError> {
        let b = [UInt8](data)   // a Data from CoreBluetooth may start at a non-zero index
        guard b.count >= 2 else { return .failure(.tooShort) }
        guard Int(b[0]) == supportedVersion else { return .failure(.unsupportedVersion(Int(b[0]))) }
        let count = Int(b[1])
        let needed = 2 + count * itemSize
        guard b.count >= needed else { return .failure(.truncated(needed: needed, got: b.count)) }
        var items: [Item] = []
        for n in 0..<count {
            let o = 2 + n * itemSize
            guard let kind = Kind(rawValue: b[o + 1]) else { return .failure(.unknownKind(b[o + 1])) }
            guard let outcome = Outcome(rawValue: b[o + 6]) else { return .failure(.unknownOutcome(b[o + 6])) }
            let bits = UInt32(b[o + 2]) | UInt32(b[o + 3]) << 8 | UInt32(b[o + 4]) << 16 | UInt32(b[o + 5]) << 24
            let value = Int32(bitPattern: bits)
            if kind == .output, value != 0, value != 1 { return .failure(.outputNotZeroOrOne(value)) }
            items.append(Item(index: Int(b[o]), kind: kind, value: value, outcome: outcome))
        }
        return .success(SelfTestFrame(items: items))
    }
}

/// One line of the self-test the person sees: a check of the plan, with what the product reported for it.
struct SelfTestRow: Equatable, Identifiable, Sendable {
    enum State: Equatable, Sendable {
        case reported(SelfTestFrame.Item)
        /// The plan lists this check and the product did not report it.
        case notReported
        /// The product reported the index as another kind than the plan says: nothing is sent for it.
        case kindMismatch
    }

    var id: Int { index }
    var index: Int
    var net: String
    var kind: CheckKind
    /// The design's range in words ("between 4.75 and 5.25 V", "on"), when the plan gives one.
    var expected: String?
    var state: State

    /// What the product measured, in words.
    var measured: String? {
        guard case .reported(let item) = state else { return nil }
        switch item.kind {
        case .rail: return String(format: "%.3f V", Double(item.value) / 1000)
        case .output: return item.value == 0 ? "off" : "on"
        }
    }

    /// The product's own verdict, for a row it reported.
    var outcome: SelfTestFrame.Outcome? {
        guard case .reported(let item) = state else { return nil }
        return item.outcome
    }
}

enum SelfTestRows {
    /// One row per check of the plan, in the plan's order. Items the plan does not list are returned apart: the app
    /// cannot say which net they are, so it shows the count and sends nothing for them.
    static func rows(frame: SelfTestFrame, plan: SelfTest) -> (rows: [SelfTestRow], unlisted: [Int]) {
        let checks = plan.checks   // the plan's order is the blink order
        var rows: [SelfTestRow] = []
        for c in checks {
            let item = frame.items.first { $0.index == c.index }
            var state = SelfTestRow.State.notReported
            if let item {
                let agrees = (item.kind == .rail) == (c.kind == .rail)
                state = agrees ? .reported(item) : .kindMismatch
            }
            rows.append(SelfTestRow(index: c.index, net: c.net, kind: c.kind, expected: expectedWords(c), state: state))
        }
        let known = Set(checks.map(\.index))
        return (rows, frame.items.map(\.index).filter { !known.contains($0) })
    }

    static func expectedWords(_ c: Check) -> String? {
        guard let e = c.expected else { return nil }
        if c.kind == .rail {
            return ExpectedReading(voltsMin: e.voltsMin, voltsMax: e.voltsMax).words
        }
        return e.state.map { $0.rawValue }
    }

    /// The readings to send for the rows the product reported, one per check. `transport` is how the app received
    /// them. A row the product did not report, or reported as another kind, sends nothing. `actionID` says which plan
    /// step a net's reading answers (nil: none).
    static func readings(rows: [SelfTestRow], transport: Transport, actionID: (String) -> String?, scanID: String, sessionID: String,
                         now: Date = Date(), id: () -> String = { "rd_" + ScanStorage.hex(8) }) -> [MeterReading] {
        rows.compactMap { row in
            guard case .reported(let item) = row.state else { return nil }
            let value = Value(volts: nil, continuity: nil, ohms: nil, index: row.index, ok: item.outcome.contract,
                              state: item.kind == .output ? (item.value == 0 ? ValueState.off : ValueState.on) : nil,
                              transport: transport, valueMv: item.kind == .rail ? Double(item.value) : nil,
                              deltaUT: nil, hz: nil, depth: nil, color: nil)
            return MeterReading(actionID: actionID(row.net), condition: nil, expected: nil, kind: .selftest, meter: nil, net: row.net,
                                part: nil, pins: nil, powerState: .on, rawText: nil, readingID: id(), recordedAt: now,
                                scanID: scanID, sessionID: sessionID, source: .selftest, value: value, verdict: nil)
        }
    }

    /// The plan's checks with the results read from the status LED, in the plan's order. The blink code carries only
    /// the product's verdict (ok, fail, unknown), no number. `problem` is set, and nothing may be sent, when the
    /// product blinked a different number of results than the plan lists: the app cannot tell which net is which.
    static func rows(blink outcomes: [SelfTestFrame.Outcome], plan: SelfTest) -> (rows: [BlinkRow], problem: String?) {
        let checks = plan.checks   // the plan's order is the blink order
        guard checks.count == outcomes.count else {
            return ([], "The light blinked \(outcomes.count) result\(outcomes.count == 1 ? "" : "s") but the design lists \(checks.count) checks, so they cannot be matched. Nothing was sent.")
        }
        let rows = zip(checks, outcomes).map { c, o in
            BlinkRow(index: c.index, net: c.net, kind: c.kind, expected: expectedWords(c), outcome: o)
        }
        return (rows, nil)
    }

    static func readings(blink rows: [BlinkRow], actionID: (String) -> String?, scanID: String, sessionID: String, now: Date = Date(),
                         id: () -> String = { "rd_" + ScanStorage.hex(8) }) -> [MeterReading] {
        rows.map { row in
            let value = Value(volts: nil, continuity: nil, ohms: nil, index: row.index, ok: row.outcome.contract, state: nil,
                              transport: .led, valueMv: nil, deltaUT: nil, hz: nil, depth: nil, color: nil)
            return MeterReading(actionID: actionID(row.net), condition: nil, expected: nil, kind: .selftest, meter: nil, net: row.net,
                                part: nil, pins: nil, powerState: .on, rawText: nil, readingID: id(), recordedAt: now,
                                scanID: scanID, sessionID: sessionID, source: .selftest, value: value, verdict: nil)
        }
    }
}

/// One check as the status LED reported it.
struct BlinkRow: Equatable, Identifiable, Sendable {
    var id: Int { index }
    var index: Int
    var net: String
    var kind: CheckKind
    var expected: String?
    var outcome: SelfTestFrame.Outcome
}
