import Foundation
import Testing
@testable import RepairOS

/// The product's self-test over Bluetooth. The bytes are hand-built from the protocol the design engine writes into the
/// product's firmware (`u8 version, u8 count`, then per check `u8 index, u8 kind, i32 little-endian value, u8 ok`); the
/// radio itself cannot run in the simulator, so `MeterLink` is not exercised here.
struct SelfTestTests {
    /// Three checks: a 4.980 V rail that is ok, a 3.300 V rail that is ok, an output that is on and failed.
    private let frameBytes: [UInt8] = [
        1, 3,
        0, 1, 0x7C, 0x13, 0x00, 0x00, 1,
        1, 1, 0xE4, 0x0C, 0x00, 0x00, 1,
        2, 2, 0x01, 0x00, 0x00, 0x00, 0,
    ]

    private func plan() throws -> SelfTest {
        try #require(try ReportModelTests.example("report.design_engine_fix_plan.json").selfTest)
    }

    // MARK: Decoding

    @Test func decodesAFrameByHand() throws {
        let frame = try SelfTestFrame.decode(Data(frameBytes)).get()
        #expect(frame.items == [
            SelfTestFrame.Item(index: 0, kind: .rail, value: 4980, outcome: .ok),
            SelfTestFrame.Item(index: 1, kind: .rail, value: 3300, outcome: .ok),
            SelfTestFrame.Item(index: 2, kind: .output, value: 1, outcome: .fail),
        ])
    }

    @Test func aRailValueIsSignedLittleEndian() throws {
        // -5 mV = 0xFFFFFFFB; 70000 mV = 0x00011170
        let frame = try SelfTestFrame.decode(Data([1, 2, 0, 1, 0xFB, 0xFF, 0xFF, 0xFF, 2, 1, 1, 0x70, 0x11, 0x01, 0x00, 1])).get()
        #expect(frame.items.map(\.value) == [-5, 70000] && frame.items.map(\.outcome) == [.unknown, .ok])
    }

    @Test func aSlicedDataAndTrailingBytesAreFine() throws {
        // CoreBluetooth can hand over a Data that starts at a non-zero index; a newer product may append bytes.
        let whole = Data([0xAA, 0xBB] + frameBytes + [0xEE])
        #expect(try SelfTestFrame.decode(whole.dropFirst(2)).get().items.count == 3)
    }

    @Test func anEmptyFrameIsAValidFrame() throws {
        #expect(try SelfTestFrame.decode(Data([1, 0])).get().items.isEmpty)
    }

    @Test func badBytesAreRefusedWithAReasonAndNothingIsGuessed() {
        func error(_ bytes: [UInt8]) -> SelfTestFrame.DecodeError? {
            if case .failure(let e) = SelfTestFrame.decode(Data(bytes)) { return e }
            return nil
        }
        #expect(error([]) == .tooShort && error([1]) == .tooShort)
        #expect(error([2, 0]) == .unsupportedVersion(2))
        #expect(error([1, 2, 0, 1, 0, 0, 0, 0, 1]) == .truncated(needed: 16, got: 9))
        #expect(error([1, 1, 0, 3, 0, 0, 0, 0, 1]) == .unknownKind(3))
        #expect(error([1, 1, 0, 1, 0, 0, 0, 0, 3]) == .unknownOutcome(3))
        #expect(error([1, 1, 0, 2, 5, 0, 0, 0, 1]) == .outputNotZeroOrOne(5))
    }

    // MARK: Rows

    @Test func eachCheckOfThePlanBecomesARowWithTheNetAndTheDesignsRange() throws {
        let frame = try SelfTestFrame.decode(Data(frameBytes)).get()
        let built = SelfTestRows.rows(frame: frame, plan: try plan())
        #expect(built.unlisted.isEmpty && built.rows.map(\.net) == ["VIN", "3V3", "LED_DRV"])
        #expect(built.rows[0].measured == "4.980 V" && built.rows[0].expected == "between 4.75 and 5.25 V")
        #expect(built.rows[0].outcome == .ok)
        #expect(built.rows[2].measured == "on" && built.rows[2].expected == "on" && built.rows[2].outcome == .fail)
    }

    @Test func aCheckTheProductSkippedAndAnIndexThePlanDoesNotListAreNeverGuessed() throws {
        let frame = SelfTestFrame(items: [
            .init(index: 0, kind: .rail, value: 5000, outcome: .ok),
            .init(index: 7, kind: .rail, value: 1200, outcome: .ok),
        ])
        let built = SelfTestRows.rows(frame: frame, plan: try plan())
        #expect(built.unlisted == [7])
        #expect(built.rows[1].state == .notReported && built.rows[2].state == .notReported)
        #expect(built.rows[1].measured == nil && built.rows[1].outcome == nil)
    }

    @Test func aKindTheProductReportsDifferentlyFromThePlanIsNotSent() throws {
        let frame = SelfTestFrame(items: [.init(index: 2, kind: .rail, value: 3300, outcome: .ok)])
        let rows = SelfTestRows.rows(frame: frame, plan: try plan()).rows
        #expect(rows[2].state == .kindMismatch)
        let readings = SelfTestRows.readings(rows: rows, transport: .ble, actionID: nil, scanID: "s", sessionID: "x")
        #expect(readings.isEmpty)
    }

    // MARK: Readings

    @Test func theReadingsSayTheyCameFromTheProductOverBluetooth() throws {
        let frame = try SelfTestFrame.decode(Data(frameBytes)).get()
        let rows = SelfTestRows.rows(frame: frame, plan: try plan()).rows
        var n = 0
        let readings = SelfTestRows.readings(rows: rows, transport: .ble, actionID: "chk_power_in", scanID: "scan_1",
                                             sessionID: "sess_1", now: Date(timeIntervalSince1970: 1_800_000_000)) {
            n += 1
            return "rd_\(n)"
        }
        #expect(readings.map(\.readingID) == ["rd_1", "rd_2", "rd_3"])
        let first = try #require(JSONSerialization.jsonObject(with: JSON.encoder.encode(readings[0])) as? [String: Any])
        #expect(first["kind"] as? String == "selftest" && first["source"] as? String == "selftest")
        #expect(first["net"] as? String == "VIN" && first["power_state"] as? String == "on" && first["action_id"] as? String == "chk_power_in")
        #expect(first["part"] == nil && first["meter"] == nil)
        let v0 = try #require(first["value"] as? [String: Any])
        #expect(v0["index"] as? Int == 0 && v0["value_mv"] as? Double == 4980 && v0["ok"] as? String == "ok" && v0["transport"] as? String == "ble")
        #expect(v0["state"] == nil)
        let third = try #require(JSONSerialization.jsonObject(with: JSON.encoder.encode(readings[2])) as? [String: Any])
        let v2 = try #require(third["value"] as? [String: Any])
        #expect(v2["state"] as? String == "on" && v2["ok"] as? String == "fail" && v2["value_mv"] == nil)
    }

    // MARK: The Bluetooth plan

    @Test func theLinkIsConfiguredFromThePlanAlone() throws {
        let config = try #require(SelfTestLinkConfig(plan: try plan()))
        #expect(config.service == "7A1E0001-5E1F-4E57-B5A1-0000C0FFEE01" && config.namePrefix == "GRAIN-")
        #expect(config.accepts(name: "GRAIN-A1B2") && !config.accepts(name: "Other") && !config.accepts(name: nil))
        #expect(config.serviceUUID.uuidString == config.service.uppercased())
    }

    @Test func aPlanWithoutAUsableBluetoothBlockMakesNoLink() throws {
        let p = try plan()
        #expect(SelfTestLinkConfig(plan: SelfTest(ble: nil, blink: p.blink, checks: p.checks, transports: [.ble])) == nil)
        #expect(SelfTestLinkConfig(plan: SelfTest(ble: p.ble, blink: p.blink, checks: p.checks, transports: [.led])) == nil)
        let bad = Ble(control: "7A1E0003-5E1F-4E57-B5A1-0000C0FFEE01", namePrefix: nil, result: "not-a-uuid",
                      service: "7A1E0001-5E1F-4E57-B5A1-0000C0FFEE01")
        #expect(SelfTestLinkConfig(plan: SelfTest(ble: bad, blink: p.blink, checks: p.checks, transports: [.ble])) == nil)
        // no name prefix in the plan: any device that advertises the service is accepted
        let open = Ble(control: bad.control, namePrefix: nil, result: "7A1E0002-5E1F-4E57-B5A1-0000C0FFEE01", service: bad.service)
        let config = try #require(SelfTestLinkConfig(plan: SelfTest(ble: open, blink: nil, checks: p.checks, transports: [.ble])))
        #expect(config.accepts(name: nil))
    }
}
