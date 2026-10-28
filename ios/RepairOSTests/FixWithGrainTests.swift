import Foundation
import Testing
@testable import RepairOS

/// A report the design engine judged (spec/contracts/examples/report.design_engine.json): its design, the meter step
/// that opens the meter sheet filled in, rows without a photo; and the reply to "Fix it with Grain".
struct FixWithGrainTests {
    @Test func aDesignEngineReportCarriesItsDesignAndTheMeterStep() throws {
        let r = try ReportModelTests.example("report.design_engine.json")
        #expect(r.design == "example_design")
        let step = try #require(r.nextStep)
        #expect(step.net == "VIN" && step.pins == ["J1.1", "J1.2"] && step.powerState == "on")
        let context = MeterReadingContext(step: step)
        #expect(context.kind == .voltageDc && context.net == "VIN" && context.pins == ["J1.1", "J1.2"])
        #expect(context.actionID == "chk_power_in")
    }

    @Test func aRowWithNoPhotoReadsWithoutACrop() throws {
        let r = try ReportModelTests.example("report.design_engine.json")
        let p = try #require(r.mostLikely.first)
        #expect(p.part == "Cover" && p.issue == "Looks missing")
        #expect(p.crops.isEmpty && p.cropStill == nil && p.point == nil)
        #expect(p.fix?.hasPrefix("Order a replacement cover") == true)
        #expect(r.notInspected.map(\.part) == ["Board"])
    }

    @Test func aReportTheDesignEngineDidNotJudgeHasNoDesign() throws {
        #expect(try ReportModelTests.example("report.json").design == nil)
        #expect(try ReportModelTests.example("report.json").nextStep?.net == nil)
    }

    @Test func theFixReplyReadsTheGatewaysAnswer() throws {
        let ok = try #require(FixReply(json: ["reply": "Moved the cover seat.", "finished": true, "waiting": false,
                                              "errors": [String](), "build": "ok"]))
        #expect(ok.reply == "Moved the cover seat." && ok.finished && !ok.waiting && ok.errors.isEmpty)
        #expect(ok.buildWords == "The design was rebuilt and every check passed.")
        let failed = try #require(FixReply(json: ["reply": "", "build": "failed", "errors": ["x"]]))
        #expect(failed.buildWords == "The design was rebuilt but a check still fails." && failed.errors == ["x"])
        #expect(try #require(FixReply(json: ["reply": "ok"])).buildWords == nil)
        #expect(FixReply(json: ["finished": true]) == nil)
    }
}
