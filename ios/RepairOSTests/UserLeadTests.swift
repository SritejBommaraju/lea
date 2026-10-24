import Foundation
import Testing
@testable import RepairOS

/// The person's guess: the live `lead` cue and the report's `user_lead` card (words from the backend).
struct UserLeadTests {
    @Test func reportSectionParsesItsThreeStates() throws {
        for (raw, want) in [("confirmed", GuessCheck.Checked.confirmed), ("not_seen", .notSeen), ("could_not_check", .couldNotCheck)] {
            let ul = try #require(GuessCheck(json: ["said": "I think it broke when I dropped it", "checked": raw,
                                                  "note": "The close photo shows no visible issue there.",
                                                  "evidence_finding_ids": ["f1"], "suspect": "back cover"]))
            #expect(ul.checked == want && ul.evidenceIDs == ["f1"] && ul.suspect == "back cover")
        }
        #expect(GuessCheck(json: ["said": "x", "checked": "maybe", "note": "n"]) == nil)
        #expect(ReportModel(json: ["verdict": "potential_issues"]).userLead == nil)
    }

    @Test @MainActor func leadEventsBecomeCuesInTheBackendsWords() {
        let live = LiveEvents(scanID: "scan_a")
        live.handle(SSEParser.Event(name: "lead", data: #"{"scan_id":"scan_a","area":"back cover","why":"you mentioned it","source_title":"","from":"you"}"#, id: "1"))
        #expect(live.lead?.spoken == "You mentioned the back cover. Let's get a close look at it.")
        live.handle(SSEParser.Event(name: "lead", data: #"{"scan_id":"scan_a","area":"hinge area","why":"w","source_title":"T","from":"web"}"#, id: "2"))
        #expect(live.lead?.spoken == "Worth a close look: the hinge area." && live.lead?.sourceTitle == "T")
        live.handle(SSEParser.Event(name: "lead", data: #"{"scan_id":"scan_other","area":"x","from":"you"}"#, id: "3"))
        #expect(live.lead?.area == "hinge area")
    }
}
