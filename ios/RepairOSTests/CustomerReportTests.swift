import Foundation
import Testing
@testable import RepairOS

/// The report as the customer reads it: the verdict in one plain sentence, confidence in words, each fix as data,
/// the "Did this fix it?" answer kept until the gateway has it, the meter check typed from any meter, and honest
/// arrival states. Pure logic only; the views are exercised by the screenshots job.
struct CustomerReportTests {
    // MARK: Helpers

    static func row(_ id: String, _ part: String, _ issue: String, confidence: String = "high") -> [String: Any] {
        ["finding_id": id, "part_class": part, "issue": issue, "confidence_word": confidence,
         "evidence": ["It is not where the design puts it."], "inspected_by": ["grain"],
         "subject": ["kind": "part", "ref": part], "proof": ["status": "not_measured"]]
    }

    static func report(_ rows: [[String: Any]], top: [String]? = nil, verdict: String = "potential_issues",
                       design: String? = "demo", notInspected: Int = 0) -> ReportModel {
        var json: [String: Any] = ["verdict": verdict, "findings": rows,
                                   "most_likely": top ?? rows.prefix(3).compactMap { $0["finding_id"] as? String },
                                   "not_inspected": (0..<notInspected).map { i in
                                       ["finding_id": "n\(i)", "part_class": "board", "reason": "inside the case",
                                        "subject": ["kind": "part", "ref": "board"]] as [String: Any]
                                   }]
        if let design { json["design"] = ["engine": "grain", "project": design] }
        return ReportModel(json: json)
    }

    static func bare(_ part: String) -> ReportModel.Problem {
        ReportModel.Problem(id: "x", part: part, issue: "", evidence: [], crops: [], photoLabels: [], regions: 1, proved: false)
    }

    // MARK: The verdict sentence

    @Test func theVerdictLeadsWithWhatIsWrongThenSaysTheRestMatches() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        #expect(CustomerWords.verdictSentence(r) == "The cover is missing. Everything else we could see matches the design.")
        let one = Self.report([Self.row("a", "rear_wheel", "missing")])
        #expect(CustomerWords.verdictSentence(one) == "The rear wheel is missing. Everything else matches the design.")
    }

    @Test func twoAndThreeProblemsAreJoinedAndTheRestAreCounted() {
        let two = Self.report([Self.row("a", "rear_wheel", "missing"), Self.row("b", "cover", "loose")])
        #expect(CustomerWords.verdictSentence(two) == "The rear wheel is missing and the cover is loose. Everything else matches the design.")
        let three = Self.report([Self.row("a", "rear_wheel", "missing"), Self.row("b", "cover", "loose"),
                                 Self.row("c", "hinge", "damaged")])
        #expect(CustomerWords.verdictSentence(three)
                == "The rear wheel is missing, the cover is loose and 1 other thing needs a look. Everything else matches the design.")
    }

    @Test func rowsBeyondTheTopThreeAreWorthALookNotMatching() {
        let rows = [Self.row("a", "rear_wheel", "missing"), Self.row("b", "cover", "loose"),
                    Self.row("c", "hinge", "damaged"), Self.row("d", "latch", "worn")]
        let r = Self.report(rows, top: ["a"])
        #expect(CustomerWords.verdictSentence(r) == "The rear wheel is missing. 3 more things are worth a look.")
    }

    @Test func partsNobodyCouldSeeAreSaidNotSkipped() {
        let r = Self.report([Self.row("a", "rear_wheel", "missing")], notInspected: 2)
        #expect(CustomerWords.verdictSentence(r) == "The rear wheel is missing. Everything else we could see matches the design.")
        let good = Self.report([], verdict: "no_problems_seen", notInspected: 2)
        #expect(CustomerWords.verdictSentence(good) == "Everything we could see matches the design. 2 parts could not be seen from outside.")
        #expect(CustomerWords.verdictSentence(Self.report([], verdict: "looks_good")) == "Everything matches the design.")
    }

    @Test func noSentenceWithoutADesignOrWithoutAPlainWay() {
        #expect(CustomerWords.verdictSentence(Self.report([Self.row("a", "rear_wheel", "missing")], design: nil)) == nil)
        #expect(CustomerWords.verdictSentence(Self.report([], verdict: "insufficient_evidence")) == nil)
    }

    @Test func aReferenceOrAcronymKeepsItsCapitals() {
        let p = Self.bare("J1")
        #expect(CustomerWords.thePart(p) == "the J1")
        var usb = p
        usb.part = "USB socket"
        #expect(CustomerWords.thePart(usb) == "the USB socket")
        usb.part = "Rear wheel"
        #expect(CustomerWords.thePart(usb) == "the rear wheel")
    }

    @Test func everyIssueCodeHasPlainWords() {
        for code in ["missing", "damaged", "broken", "shifted", "partly", "loose", "unseated", "reversed", "worn", "dirty",
                     "open", "short", "out_of_range", "wrong_pin", "unexpected_part", "unexpected_wire", "colour_mismatch"] {
            #expect(!CustomerWords.predicate(code).contains("_") && CustomerWords.predicate(code) != "may have a problem")
        }
        #expect(CustomerWords.predicate("something_new") == "may have a problem")
    }

    // MARK: Confidence in words

    @Test func confidenceIsAWordNeverANumber() {
        #expect(CustomerWords.confidence("high") == "Likely")
        #expect(CustomerWords.confidence("medium") == "Possible")
        #expect(CustomerWords.confidence("low") == "Unsure")
        #expect(CustomerWords.confidence(nil) == nil && CustomerWords.confidence("0.9") == nil)
        #expect(CustomerWords.confidence("low", proved: true) == "Confirmed with a meter")
        #expect(CustomerWords.confidenceLine("high") == "How sure we are: likely.")
        #expect(CustomerWords.confidenceLine("low", proved: true) == "Confirmed with a meter.")
    }

    // MARK: Fix plans and outcomes in the report

    @Test func aRowCarriesItsFixAsDataAndItsSavedAnswer() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        let p = try #require(r.mostLikely.first)
        let plan = try #require(p.fixPlan)
        #expect(plan.action == .buy && plan.summary == "Order a replacement cover.")
        #expect(plan.offers.count == 1 && plan.offers[0].seller == "example-parts.test")
        #expect(plan.offers[0].link.absoluteString == "https://example-parts.test/cover")
        #expect(p.outcome == OutcomeAnswer(choice: .fixed, recordedAt: "2026-10-28T12:00:00Z", note: "New cover clipped on."))
        #expect(p.issueCode == "missing" && p.confidenceWord == "high")
    }

    @Test func aRemakePlanListsOnlyTheFormatsTheGatewayServes() throws {
        let plan = try #require(RepairFix(json: ["action": "remake", "part_file": ["formats": ["stl", "step", "exe"]],
                                               "steps": ["Print it flat.", "  "]]))
        #expect(plan.action == .remake && plan.fileFormats == ["stl", "step"] && plan.steps == ["Print it flat."])
        #expect(RepairFix(json: ["action": "teleport"]) == nil && RepairFix(json: nil) == nil)
        let check = try #require(RepairFix(json: ["action": "check", "check_action_id": "chk_vin"]))
        #expect(check.checkActionID == "chk_vin" && check.offers.isEmpty)
    }

    @Test func anOfferWithoutAShopLinkIsDropped() throws {
        let plan = try #require(RepairFix(json: ["action": "buy", "options": [
            ["title": "Cover", "seller": "Shop", "link": "javascript:alert(1)"],
            ["title": "Cover", "seller": "Shop", "link": "https://shop.example/cover", "price": 4.5, "currency": "USD"]]]))
        #expect(plan.offers.map(\.link.absoluteString) == ["https://shop.example/cover"])
    }

    @Test func whatTheScanSawNamesItsSource() {
        var p = Self.bare("Cover")
        #expect(CustomerWords.evidenceSource(p) == "From the scan, compared with the design")
        p.photoLabels = ["Photo 3", "Photo 4"]
        #expect(CustomerWords.evidenceSource(p) == "From Photo 3, Photo 4")
        p.proved = true
        #expect(CustomerWords.evidenceSource(p) == "From a meter reading")
    }

    @Test func fixItWithGrainShowsWhatItWillBeTold() throws {
        let r = try ReportModelTests.example("report.design_engine_fix_plan.json")
        let lines = CustomerWords.fixContext(r, symptom: "no output", readingsSent: 1)
        #expect(lines.first == "The design: example_design")
        #expect(lines.contains("What you told us: \u{201C}no output\u{201D}"))
        #expect(lines.contains("What the scan found: the cover is missing"))
        #expect(lines.contains("1 meter reading and what the design says about it"))
        #expect(lines.last?.hasPrefix("Fixes suggested so far: Order a replacement cover") == true)
        #expect(!CustomerWords.fixContext(r, symptom: nil, readingsSent: 0).contains { $0.contains("meter reading") })
    }

    // MARK: The answers, kept until sent

    private static let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    @Test func aNewerAnswerReplacesTheOlderAndOnlyTheSentOneIsMarkedSent() {
        var box = OutcomeOutbox()
        box.record(rowID: "r1", choice: .notFixed, at: Self.t0)
        let first = box.entries["r1"]!
        box.record(rowID: "r1", choice: .fixed, note: "  Works.  ", at: Self.t0.addingTimeInterval(5))
        box.markSent(first)   // the answer that went out was superseded meanwhile: it must not mark the new one sent
        #expect(box.entries["r1"]?.choice == .fixed && box.entries["r1"]?.note == "Works." && box.entries["r1"]?.sent == false)
        box.markSent(box.entries["r1"]!)
        #expect(box.entries["r1"]?.sent == true && box.unsent.isEmpty)
    }

    @Test func unsentAnswersGoOldestFirstAndSurviveARestart() throws {
        var box = OutcomeOutbox()
        box.record(rowID: "b", choice: .fixed, at: Self.t0.addingTimeInterval(10))
        box.record(rowID: "a", choice: .notTried, at: Self.t0)
        #expect(box.unsent.map(\.rowID) == ["a", "b"])
        let again = try JSON.decoder.decode(OutcomeOutbox.self, from: JSON.encoder.encode(box))
        #expect(again == box)
    }

    @Test func anOfflineErrorIsAWaitAndAnythingElseIsTheGatewaysReason() {
        #expect(OutcomeStatus.after(URLError(.notConnectedToInternet)) == .queued)
        #expect(OutcomeStatus.after(URLError(.timedOut)).words == "Will send when you are online.")
        if case .failed(let why) = OutcomeStatus.after(GatewayError.http(404, #"{"detail":"no row"}"#)) {
            #expect(why.contains("404") && why.contains("no row"))
        } else {
            Issue.record("a 404 is not an offline error")
        }
        #expect(OutcomeStatus.saved.words == "Saved. Thank you." && OutcomeStatus.sending.words == "Sending\u{2026}")
    }

    @MainActor
    @Test func anAnswerGivenOfflineIsQueuedOnThePhoneAndStillThereAfterARestart() async throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "outcome-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let board = OutcomeBoard()
        board.configure(scanID: "scan_x", directory: dir, gateway: nil)
        #expect(board.answer(rowID: "r1", server: nil) == nil)
        board.set(.fixed, rowID: "r1")
        await board.flush()
        #expect(board.answer(rowID: "r1", server: nil) == .fixed && board.status["r1"] == .queued)
        // the phone's own answer wins over an older one the gateway gave back
        #expect(board.answer(rowID: "r1", server: OutcomeAnswer(choice: .notFixed)) == .fixed)
        #expect(board.answer(rowID: "r2", server: OutcomeAnswer(choice: .notFixed)) == .notFixed)
        board.stop()

        let reopened = OutcomeBoard()
        reopened.configure(scanID: "scan_x", directory: dir, gateway: nil)
        #expect(reopened.answer(rowID: "r1", server: nil) == .fixed && reopened.status["r1"] == .queued)
        reopened.stop()
    }

    @Test func anAnswerFollowsItsPartWhenTheReportIsMadeAgain() {
        var box = OutcomeOutbox()
        box.record(rowID: "g_wheel_old", key: "part:rear_wheel", choice: .fixed, at: Self.t0)
        let sameID = box.adopt(rowID: "g_wheel_old", key: "part:rear_wheel")   // same id: nothing moves
        let otherPart = box.adopt(rowID: "g_other", key: "part:cover")          // another part: nothing moves
        let moved = box.adopt(rowID: "g_wheel_new", key: "part:rear_wheel")
        #expect(!sameID && !otherPart && moved)
        #expect(box.entries["g_wheel_old"] == nil && box.entries["g_wheel_new"]?.choice == .fixed)
        #expect(box.unsent.map(\.rowID) == ["g_wheel_new"])   // it is sent under the row the gateway has now
        // a newer answer for the same part under a new id replaces the older one
        box.record(rowID: "g_wheel_3", key: "part:rear_wheel", choice: .notFixed, at: Self.t0.addingTimeInterval(9))
        #expect(box.entries.count == 1 && box.entries["g_wheel_3"]?.choice == .notFixed)
        // an outbox written before keys existed still reads
        let old = try? JSON.decoder.decode(OutcomeOutbox.self, from: Data(
            #"{"entries":{"r":{"rowID":"r","choice":"fixed","at":"2027-01-15T08:00:00Z","sent":false}}}"#.utf8))
        #expect(old?.entries["r"]?.key == nil)
    }

    @MainActor
    @Test func aRowThatMovedShowsTheAnswerGivenBeforeItMoved() async throws {
        let board = OutcomeBoard()
        board.configure(scanID: "scan_x", directory: nil, gateway: nil)
        board.set(.fixed, rowID: "old", key: "part:cover")
        board.adopt(rowID: "new", key: "part:cover")
        #expect(board.answer(rowID: "new", server: nil) == .fixed && board.answer(rowID: "old", server: nil) == nil)
        board.stop()
    }

    @Test func everyFlaggedRowKeepsItsFixNotOnlyTheTopThree() throws {
        let plan: [String: Any] = ["action": "reseat", "summary": "Press it down."]
        let rows = (1...4).map { i -> [String: Any] in
            var r = Self.row("r\(i)", "part\(i)", "loose")
            r["fix_plan"] = plan
            return r
        }
        let r = Self.report(rows, top: ["r1", "r2", "r3"])
        #expect(r.otherIssues.count == 1)
        #expect((r.mostLikely + r.otherIssues).allSatisfy { $0.fixPlan?.action == .reseat })
        #expect(r.otherIssues[0].subjectKey == "part:part4")
    }

    @Test func aSuspectNobodyMeasuredIsNeverSaidAsAFact() {
        let open = Self.report([Self.row("a", "wire", "open", confidence: "medium")])
        #expect(CustomerWords.verdictSentence(open) == "The wire may have a broken connection. Everything else matches the design.")
        let unsure = Self.report([Self.row("a", "rear_wheel", "missing", confidence: "low")])
        #expect(CustomerWords.verdictSentence(unsure) == "The rear wheel may be missing. Everything else matches the design.")
        var proved = Self.row("a", "wire", "open")
        proved["proof"] = ["status": "confirmed_by_meter", "reading_id": "rd"]
        #expect(CustomerWords.verdictSentence(Self.report([proved])) == "The wire has a broken connection. Everything else matches the design.")
    }

    @Test func aCheckThatCouldNotBeSettledIsNotCalledAMatch() {
        var json: [String: Any] = ["verdict": "potential_issues", "design": ["engine": "grain", "project": "demo"],
                                   "findings": [Self.row("a", "rear_wheel", "missing")], "most_likely": ["a"],
                                   "insufficient_evidence": [["finding_id": "i", "part_class": "net", "reason": "no expected value",
                                                              "subject": ["kind": "net", "ref": "VIN"]]]]
        #expect(CustomerWords.verdictSentence(ReportModel(json: json)) == "The rear wheel is missing. 1 check could not be completed.")
        json["verdict"] = "no_problems_seen"
        json["findings"] = [[String: Any]]()
        json["most_likely"] = [String]()
        #expect(CustomerWords.verdictSentence(ReportModel(json: json)) == "Nothing looked wrong, but 1 check could not be completed.")
    }

    @Test func theChoicesAreTheContractsStatesInWords() {
        #expect(OutcomeChoice.allCases.map(\.rawValue) == ["fixed", "not_fixed", "not_tried"])
        #expect(OutcomeChoice.allCases.map(\.button) == ["Yes", "No", "Haven't tried"])
    }

    // MARK: The meter check, typed from any meter

    @Test func aCheckFromTheReportOpensGuidedAndAFreeSheetDoesNot() throws {
        let r = try ReportModelTests.example("report.design_engine.json")
        let ctx = MeterReadingContext(step: try #require(r.nextStep))
        #expect(ctx.isGuided && ctx.actionID == "chk_power_in" && ctx.kind == .voltageDc)
        #expect(ctx.expected?.words == "between 4.75 and 5.25 V")
        #expect(!MeterReadingContext().isGuided)
        #expect(!MeterReadingContext(kind: .voltageDc, net: "VIN", pins: ["J1.1", "J1.2"]).isGuided)   // no check id
    }

    @Test func aTypedReadingCarriesTheChecksIdAndGoesAsTyped() throws {
        let r = try ReportModelTests.example("report.design_engine.json")
        let ctx = MeterReadingContext(step: try #require(r.nextStep))
        let value = try #require(MeterReadingDraft.value(typed: "4,98", kind: .voltageDc, beeps: true))
        let reading = MeterReadingDraft.reading(context: ctx, kind: ctx.kind, net: ctx.net, pinA: ctx.pins[0], pinB: ctx.pins[1],
                                                scanID: "scan_x", sessionID: "sess_x", value: value, meter: nil, rawText: "4,98",
                                                now: Self.t0, id: "rd_hand")
        let json = try #require(try JSONSerialization.jsonObject(with: JSON.encoder.encode(reading)) as? [String: Any])
        #expect(json["source"] as? String == "typed" && json["action_id"] as? String == "chk_power_in")
        #expect(json["power_state"] as? String == "on" && json["kind"] as? String == "voltage_dc")
        #expect(json["pins"] as? [String] == ["J1.1", "J1.2"] && json["net"] as? String == "VIN")
        #expect(json["meter"] == nil && json["raw_text"] as? String == "4,98")
        #expect((json["value"] as? [String: Any])?["volts"] as? Double == 4.98)
        #expect(MeterReadingDraft.value(typed: "four", kind: .voltageDc, beeps: true) == nil)
    }

    @Test func probePointsAreSaidInPlainWords() {
        #expect(MeterWords.pin("J1.1") == "J1, pin 1" && MeterWords.pin("GND") == "GND" && MeterWords.pin("U1.") == "U1.")
        #expect(MeterWords.probes(pins: ["J1.1", "J1.2"], kind: .voltageDc) == ["Red probe on J1, pin 1", "Black probe on J1, pin 2"])
        #expect(MeterWords.probes(pins: ["J1.2", "U1.4"], kind: .continuity)
                == ["Touch one probe on J1, pin 2", "Touch the other probe on U1, pin 4"])
        #expect(MeterWords.probes(pins: ["J1.1"], kind: .continuity).isEmpty)
        #expect(MeterWords.unit(.voltageDc) == "V" && MeterWords.unit(.continuity) == nil)
    }

    @Test func theExpectedRangeIsSaidBeforeMeasuring() {
        #expect(ExpectedReading(voltsMin: 4.75, voltsMax: 5.25).words == "between 4.75 and 5.25 V")
        #expect(ExpectedReading(voltsMin: 3).words == "3 V or more")
        #expect(ExpectedReading(ohmsMax: 1).words == "1 \u{3A9} or less")
        #expect(ExpectedReading(continuity: true).words == "the meter should beep (a connection)")
        #expect(ExpectedReading(json: ["source": "design_engine"]) == nil)
        #expect(ExpectedReading.number(0.1 + 0.2) == "0.3" && ExpectedReading.number(5) == "5")
    }

    @Test func theVerdictIsPassFailOrNeitherAndNeverGuessed() {
        let range = ExpectedReading(voltsMin: 4.75, voltsMax: 5.25)
        let ok = ReadingVerdict(state: "judged", verdict: "ok")
        #expect(ok.outcome == .pass && ok.headline(expected: range) == "Matches the design. It should be between 4.75 and 5.25 V.")
        let off = ReadingVerdict(state: "judged", verdict: "off")
        #expect(off.outcome == .fail && off.headline(expected: range) == "Does not match the design. It should be between 4.75 and 5.25 V.")
        #expect(off.headline(expected: nil) == "Does not match the design.")
        #expect(ReadingVerdict(state: "judged", verdict: "open").outcome == .fail)
        #expect(ReadingVerdict(state: "judged", verdict: "no_expected").outcome == .unjudged)
        #expect(ReadingVerdict(state: "pending").outcome == .unjudged && !ReadingVerdict(state: "pending").isFinal)
        #expect(ReadingVerdict(state: "failed").isFinal && ReadingVerdict(state: "unavailable").isFinal)
        #expect(ReadingVerdict(state: "pending").headline(expected: range).hasPrefix("Saved."))
        let parsed = ReadingVerdict(json: ["state": "judged", "verdict": "off", "notes": ["VIN reads 3.1 V.", " "]])
        #expect(parsed == ReadingVerdict(state: "judged", verdict: "off", notes: ["VIN reads 3.1 V."]))
        #expect(ReadingVerdict(json: [:]) == nil)
    }

    // MARK: The report on its way

    @Test func theWaitSaysWhatIsHappeningAndAdmitsWhenItIsLong() {
        typealias A = ReportArrival
        #expect(A.state(step: .upload, uploaded: 2, pending: 4, waited: 0, error: nil).title == "Sending your scan: 3 of 6")
        #expect(A.state(step: .upload, uploaded: 0, pending: 0, waited: 0, error: nil).title == "Sending your scan")
        let looking = A.state(step: .judge, uploaded: 6, pending: 0, waited: 30, error: nil)
        #expect(looking.phase == .looking && looking.title == "Uploaded. Grain is looking at it.")
        #expect(looking.detail?.contains("leave this screen") == true)
        let slow = A.state(step: .judge, uploaded: 6, pending: 0, waited: 6 * 60 + 10, error: nil)
        #expect(slow.phase == .slow && slow.title == "Taking longer than usual" && slow.detail?.contains("6 minutes") == true)
        #expect(A.state(step: .judge, uploaded: 6, pending: 0, waited: A.slowAfter - 1, error: nil).phase == .looking)
        let failed = A.state(step: .judge, uploaded: 6, pending: 0, waited: 10, error: "the design engine is down")
        #expect(failed.phase == .failed && failed.detail == "the design engine is down")
    }

    @Test func theDesignEnginesStageIsTheJudgeStep() {
        var p = PipelineProgress()
        p.update(["stages": ["design_engine": ["status": "running"]]])
        #expect(p.step == .judge)
        p.update(["stages": ["design_engine": ["status": "pending"]]])
        #expect(p.step == .inspect)   // a stage that has not started does not count
        #expect(PipelineProgress.Step.judge.title == "Grain is looking at it")
    }
}
