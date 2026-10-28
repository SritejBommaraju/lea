import Foundation
import Testing
@testable import RepairOS

/// The typed meter reading and the sheet's context. The product's self-test, the blink code and the phone's sensors
/// have their own files (SelfTestTests, BlinkDecoderTests, SpectrumTests, PhoneSensorTests).
struct MeterTests {
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

    @Test func typedValuesAcceptACommaAndRefuseNegativeOhms() {
        #expect(MeterReadingDraft.value(typed: "4,98", kind: .voltageDc, beeps: true)?.volts == 4.98)
        #expect(MeterReadingDraft.value(typed: "-3", kind: .resistance, beeps: true) == nil)
        #expect(MeterReadingDraft.value(typed: "abc", kind: .voltageDc, beeps: true) == nil)
        #expect(MeterReadingDraft.value(typed: "", kind: .continuity, beeps: false)?.continuity == false)
    }

    @Test func aTypedReadingGoesAsTypedWithNoMeterAndEncodesToTheContract() throws {
        let value = try #require(MeterReadingDraft.value(typed: "4,98", kind: .voltageDc, beeps: true))
        let context = MeterReadingContext(kind: .voltageDc, net: "VBUS", pins: ["J1.A4", "J1.A1"], actionID: "act_1")
        let r = MeterReadingDraft.reading(context: context, kind: .voltageDc, net: "VBUS", pinA: "J1.A4", pinB: "J1.A1",
                                          scanID: "scan_1", sessionID: "sess_1", value: value, rawText: "4,98",
                                          now: Date(timeIntervalSince1970: 1_800_000_001), id: "rd_test")
        #expect(r.source == .typed && r.powerState == .on && r.actionID == "act_1" && r.meter == nil && r.part == nil)
        let json = try #require(JSONSerialization.jsonObject(with: JSON.encoder.encode(r)) as? [String: Any])
        #expect(json["source"] as? String == "typed" && json["kind"] as? String == "voltage_dc")
        #expect(json["pins"] as? [String] == ["J1.A4", "J1.A1"] && json["power_state"] as? String == "on")
        #expect(json["meter"] == nil && json["part"] == nil)
        #expect((json["value"] as? [String: Any])?["volts"] as? Double == 4.98)
    }

    @Test func aTypedResistanceHasNoMeterObject() throws {
        let value = try #require(MeterReadingDraft.value(typed: "12", kind: .resistance, beeps: true))
        let r = MeterReadingDraft.reading(context: MeterReadingContext(), kind: .resistance, net: "N", pinA: "A.1", pinB: "B.1",
                                          scanID: "s", sessionID: "x", value: value, rawText: "12")
        #expect(r.source == .typed && r.meter == nil && r.powerState == .off && r.kind == .resistance)
    }

    @Test func contextComesFromAReportStep() {
        let step = ReportModel.NextStep(kind: "run_voltage_test", instruction: "Probe it", reason: nil, actionID: "act_9")
        let c = MeterReadingContext(step: step)
        #expect(c.kind == .voltageDc && c.actionID == "act_9" && c.net.isEmpty)
    }

    @Test func multimeterKindsMapToTheContractKinds() {
        #expect(MultimeterKind.voltageDc.contract == .voltageDc)
        #expect(MultimeterKind.continuity.contract == .continuity && MultimeterKind.resistance.contract == .resistance)
    }

    // MARK: Which readings answer the step

    @Test func aReadingCarriesTheStepsIdOnlyWhenItAnswersTheStep() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        let c = MeterReadingContext(step: try #require(r.nextStep), selfTest: r.selfTest)
        #expect(c.answeringActionID(for: .net("VIN")) == "chk_power_in")
        #expect(c.answeringActionID(for: .net(" vin ")) == "chk_power_in")
        #expect(c.answeringActionID(for: .net("3V3")) == nil)                       // another net: a separate measurement
        #expect(c.answeringActionID(for: .part("M1", .audio)) == nil)               // the step names the self-test, not a tone
        #expect(MeterReadingContext().answeringActionID(for: .net("VIN")) == nil)

        let sensor = MeterReadingContext(actionID: "chk_m", instrument: .magnetometer, parts: ["M1"])
        #expect(sensor.answeringActionID(for: .part("m1", .magnetometer)) == "chk_m")
        #expect(sensor.answeringActionID(for: .part("M2", .magnetometer)) == nil)
        #expect(sensor.answeringActionID(for: .part("M1", .audio)) == nil)
        #expect(sensor.answeringActionID(for: .net("VIN")) == nil)
        // a step that names the sensor but no part is answered by any part
        #expect(MeterReadingContext(actionID: "chk_a", instrument: .audio).answeringActionID(for: .part("FAN", .audio)) == "chk_a")
    }

    @Test func aTypedReadingOnAnotherNetThanTheStepsDoesNotAnswerIt() throws {
        let context = MeterReadingContext(kind: .voltageDc, net: "VBUS", pins: ["J1.1", "J1.2"], actionID: "act_1")
        let value = try #require(MeterReadingDraft.value(typed: "4.9", kind: .voltageDc, beeps: true))
        func reading(net: String) -> MeterReading {
            MeterReadingDraft.reading(context: context, kind: .voltageDc, net: net, pinA: "J1.1", pinB: "J1.2", scanID: "s",
                                      sessionID: "x", value: value, rawText: "4.9")
        }
        #expect(reading(net: "VBUS").actionID == "act_1" && reading(net: "vbus").actionID == "act_1")
        #expect(reading(net: "OTHER").actionID == nil)
    }

    // MARK: Which section opens first

    @Test func aStepNamingAnInstrumentOpensThatSectionIfTheProductOffersIt() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        let step = try #require(r.nextStep)
        #expect(step.instrument == "selftest_ble")
        let c = MeterReadingContext(step: step, selfTest: r.selfTest)
        #expect(c.instrument == .selftestBle && c.firstInstrument == .selftestBle)

        // The same step on a product with no Bluetooth block: the self-test over the light opens instead.
        var noBle = try #require(r.selfTest)
        noBle = SelfTest(ble: nil, blink: noBle.blink, checks: noBle.checks, transports: [.led])
        let c2 = MeterReadingContext(step: step, selfTest: noBle)
        #expect(c2.firstInstrument == .selftestLED)
    }

    @Test func aPhoneSensorStepOpensItsSectionWithOrWithoutASelfTest() {
        let c = MeterReadingContext(instrument: .magnetometer, parts: ["M1"])
        #expect(c.firstInstrument == .magnetometer && c.parts == ["M1"])
        #expect(MeterReadingContext().firstInstrument == nil)
        #expect(!MeterReadingContext().offers(.selftestBle) && MeterReadingContext().offers(.audio))
    }

    @Test func theReportCarriesTheSelfTestPlanIntoTheSheetContext() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        let plan = try #require(r.selfTest)
        #expect(plan.transports == [.ble, .led] && plan.checks.map(\.index) == [0, 1, 2])
        #expect(plan.ble?.service == "7A1E0001-5E1F-4E57-B5A1-0000C0FFEE01" && plan.ble?.namePrefix == "GRAIN-")
        #expect(plan.blink?.timings.longOnMS == 600 && plan.blink?.timings.gapMS == 1000)
        #expect(r.meterContext.selfTest?.checks.count == 3 && r.meterContext.instrument == .selftestBle)
        // a report with no self_test block has none
        #expect(try ReportModelTests.example("report.design_engine.json").selfTest == nil)
    }
}
