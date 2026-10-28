import Foundation
import Testing
@testable import RepairOS

/// The Pokit Meter's bytes, hand-built from its published Bluetooth API (D0005250 rev 0.02). No radio involved.
struct MeterTests {
    let pokit = PokitMeterProfile()

    private func le(_ f: Float) -> [UInt8] {
        let bits = f.bitPattern
        return (0..<4).map { UInt8((bits >> UInt32(8 * $0)) & 0xFF) }
    }

    @Test func settingsForContinuityAutoRange500ms() {
        // mode 7 (continuity), range 255 (auto), 500 ms = 0x000001F4 little-endian
        #expect([UInt8](pokit.settingsPayload(mode: .continuity, range: pokit.autoRange, intervalMs: 500))
                == [0x07, 0xFF, 0xF4, 0x01, 0x00, 0x00])
    }

    @Test func settingsForIdleStopsTheMeter() {
        #expect([UInt8](pokit.settingsPayload(mode: .idle, range: 0, intervalMs: 1000)) == [0x00, 0x00, 0xE8, 0x03, 0x00, 0x00])
    }

    @Test func parsesADCVoltageReading() throws {
        // status 1 (auto-range on), 4.98 V as float32 LE, mode 1 (DC voltage), range 2 (2-6 V)
        let data = Data([0x01] + le(4.98) + [0x01, 0x02])
        let s = try #require(pokit.parseReading(data))
        #expect(s.mode == .dcVoltage && s.autoRange && !s.error && s.continuity == nil)
        #expect(abs(s.value - 4.98) < 1e-6)
        #expect(s.rangeLabel == "2-6 V")
    }

    @Test func parsesContinuity() throws {
        let on = try #require(pokit.parseReading(Data([0x01, 0, 0, 0, 0, 0x07, 0xFF])))
        #expect(on.mode == .continuity && on.continuity == true && !on.error)
        let open = try #require(pokit.parseReading(Data([0x00, 0, 0, 0, 0, 0x07, 0xFF])))
        #expect(open.continuity == false)
    }

    @Test func errorStatusIsNeverAValue() throws {
        let s = try #require(pokit.parseReading(Data([0xFF, 0, 0, 0xC0, 0x7F, 0x05, 0x03])))
        #expect(s.error && s.mode == .resistance && s.continuity == nil)
    }

    @Test func rejectsShortOrUnknownReadings() {
        #expect(pokit.parseReading(Data([0x01, 0, 0])) == nil)
        #expect(pokit.parseReading(Data([0x01, 0, 0, 0, 0, 0x63, 0x00])) == nil)   // no such mode
    }

    @Test func readsASlicedData() throws {
        // CoreBluetooth can hand over a Data that starts at a non-zero index.
        let whole = Data([0xAA, 0xBB, 0x01] + le(1.0) + [0x01, 0x00])
        let s = try #require(pokit.parseReading(whole.dropFirst(2)))
        #expect(s.value == 1.0 && s.mode == .dcVoltage && s.rangeLabel == "0-300 mV")
    }

    @Test func batteryVoltageComesFromTheLastFourBytes() {
        #expect(abs((pokit.parseBattery(Data([0x01, 0x02, 0x03] + le(3.1))) ?? 0) - 3.1) < 1e-6)
        #expect(pokit.parseBattery(Data([0x01, 0x02])) == nil)
        #expect(pokit.parseBattery(Data([0, 0, 0xC0, 0x7F])) == nil)   // NaN
    }

    @Test func kindsMapToModes() {
        #expect(MeterMode(kind: .voltageDc) == .dcVoltage && MeterMode(kind: .continuity).contract == .continuity)
        #expect(MeterMode.idle.contract == nil)
    }

    // MARK: The reading the sheet builds

    @Test func powerStateFollowsTheKindAndIsShownFirst() {
        #expect(MeterReadingDraft.powerState(.continuity) == .off && MeterReadingDraft.powerState(.resistance) == .off)
        #expect(MeterReadingDraft.powerState(.voltageDc) == .on)
        #expect(MeterReadingDraft.banner(.resistance).hasPrefix("Switch the product off"))
        #expect(MeterReadingDraft.banner(.voltageDc) == "The product is powered: keep the probes apart.")
    }

    @Test func probePointsAreRequiredWhereTheContractRequiresThem() {
        #expect(MeterReadingDraft.problem(kind: .continuity, net: "VBUS", pinA: "", pinB: "") != nil)
        #expect(MeterReadingDraft.problem(kind: .voltageDc, net: "VBUS", pinA: "", pinB: "") == nil)
        #expect(MeterReadingDraft.problem(kind: .voltageDc, net: "VBUS", pinA: "J1.1", pinB: "") != nil)
        #expect(MeterReadingDraft.problem(kind: .resistance, net: " ", pinA: "J1.1", pinB: "R1.2") != nil)
        #expect(MeterReadingDraft.problem(kind: .resistance, net: "VBUS", pinA: "J1.1", pinB: "R1.2") == nil)
    }

    @Test func aSampleOfAnotherModeOrAnErrorIsNotUsed() throws {
        let volts = try #require(pokit.parseReading(Data([0x01] + le(4.98) + [0x01, 0x02])))
        #expect(MeterReadingDraft.value(from: volts, kind: .voltageDc)?.volts == Double(Float(4.98)))
        #expect(MeterReadingDraft.value(from: volts, kind: .resistance) == nil)
        let bad = try #require(pokit.parseReading(Data([0xFF] + le(0) + [0x01, 0x00])))
        #expect(MeterReadingDraft.value(from: bad, kind: .voltageDc) == nil)
    }

    @Test func typedValuesAcceptACommaAndRefuseNegativeOhms() {
        #expect(MeterReadingDraft.value(typed: "4,98", kind: .voltageDc, beeps: true)?.volts == 4.98)
        #expect(MeterReadingDraft.value(typed: "-3", kind: .resistance, beeps: true) == nil)
        #expect(MeterReadingDraft.value(typed: "abc", kind: .voltageDc, beeps: true) == nil)
        #expect(MeterReadingDraft.value(typed: "", kind: .continuity, beeps: false)?.continuity == false)
    }

    @Test func aBluetoothReadingCarriesTheMeterAndEncodesToTheContract() throws {
        let sample = try #require(pokit.parseReading(Data([0x01] + le(4.98) + [0x01, 0x02]), at: Date(timeIntervalSince1970: 1_800_000_000)))
        let value = try #require(MeterReadingDraft.value(from: sample, kind: .voltageDc))
        let meter = MeterReadingDraft.meter(sample: sample, model: "Pokit Meter", firmware: "1.0", batteryV: 3.1, intervalMs: 500)
        let context = MeterReadingContext(kind: .voltageDc, net: "VBUS", pins: ["J1.A4", "J1.A1"], actionID: "act_1")
        let r = MeterReadingDraft.reading(context: context, kind: .voltageDc, net: "VBUS", pinA: "J1.A4", pinB: "J1.A1",
                                          scanID: "scan_1", sessionID: "sess_1", value: value, meter: meter, rawText: nil,
                                          now: Date(timeIntervalSince1970: 1_800_000_001), id: "rd_test")
        #expect(r.source == .ble && r.powerState == .on && r.actionID == "act_1")
        let json = try #require(JSONSerialization.jsonObject(with: JSON.encoder.encode(r)) as? [String: Any])
        #expect(json["source"] as? String == "ble")
        let m = try #require(json["meter"] as? [String: Any])
        #expect(m["mode"] as? String == "dc_voltage" && m["range"] as? String == "auto (2-6 V)" && m["interval_ms"] as? Int == 500)
        #expect(m["model"] as? String == "Pokit Meter" && m["battery_v"] as? Double == 3.1 && m["read_at"] is String)
        #expect(json["pins"] as? [String] == ["J1.A4", "J1.A1"] && json["power_state"] as? String == "on")
    }

    @Test func aTypedReadingHasNoMeterObject() throws {
        let value = try #require(MeterReadingDraft.value(typed: "12", kind: .resistance, beeps: true))
        let r = MeterReadingDraft.reading(context: MeterReadingContext(), kind: .resistance, net: "N", pinA: "A.1", pinB: "B.1",
                                          scanID: "s", sessionID: "x", value: value, meter: nil, rawText: "12")
        #expect(r.source == .typed && r.meter == nil && r.powerState == .off)
    }

    @Test func contextComesFromAReportStep() {
        let step = ReportModel.NextStep(kind: "run_voltage_test", instruction: "Probe it", reason: nil, actionID: "act_9")
        let c = MeterReadingContext(step: step)
        #expect(c.kind == .voltageDc && c.actionID == "act_9" && c.net.isEmpty)
    }
}
