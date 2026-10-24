import Foundation
import Testing
@testable import RepairOS

/// The report screen's polling decision: never blank a shown report, fetch only newer revisions.
struct ReportPollTests {
    typealias A = ReportPoll.Action

    @Test func firstScanWaitsThenFetches() {
        #expect(ReportPoll.decide(showingRev: nil, status: "inspecting", reportRev: nil, rebuilding: false) == .wait(updating: false))
        #expect(ReportPoll.decide(showingRev: nil, status: "done", reportRev: 1, rebuilding: false) == .fetch)
        // An older gateway without report_rev: "done" still fetches.
        #expect(ReportPoll.decide(showingRev: nil, status: "done", reportRev: nil, rebuilding: false) == .fetch)
    }

    @Test func continuationKeepsTheReportWithTheChip() {
        #expect(ReportPoll.decide(showingRev: 1, status: "inspecting", reportRev: 1, rebuilding: true) == .wait(updating: true))
        #expect(ReportPoll.decide(showingRev: 1, status: "done", reportRev: 2, rebuilding: false) == .fetch)
        // Rev 2 finished while rev 3 is already pending: fetch 2 now, keep updating.
        #expect(ReportPoll.decide(showingRev: 1, status: "inspecting", reportRev: 2, rebuilding: true) == .fetch)
        #expect(ReportPoll.decide(showingRev: 2, status: "done", reportRev: 2, rebuilding: false) == .settle)
    }

    @Test func closedOrFailedLeaveTheCurrentReport() {
        #expect(ReportPoll.decide(showingRev: 1, status: "closed", reportRev: 1, rebuilding: false) == .settle)
        #expect(ReportPoll.decide(showingRev: 1, status: "failed", reportRev: 1, rebuilding: false) == .settle)
        #expect(ReportPoll.decide(showingRev: nil, status: "failed", reportRev: nil, rebuilding: false, error: "inspect") == .fail("inspect"))
    }

    @Test func cachedReportOfUnknownRevisionRefreshesOnce() {
        // A report cached before revisions were stored counts as 0: the gateway's rev 1 replaces it.
        #expect(ReportPoll.decide(showingRev: 0, status: "done", reportRev: 1, rebuilding: false) == .fetch)
    }
}

/// The progress steps read each stage's status: a run lists every stage as `pending` from its start (Greptile, PR #7).
struct PipelineProgressTests {
    func view(_ stages: [String: String], stills: [String: String] = [:]) -> [String: Any] {
        ["stages": stages.mapValues { ["status": $0] }, "stills": stills.mapValues { ["status": $0] }]
    }

    @Test func stepFollowsTheStageStatusNotItsPresence() {
        var p = PipelineProgress()
        p.update(view(["select": "done", "inspect": "running", "fuse": "pending", "locate": "pending", "report": "pending"],
                      stills: ["s1": "done", "s2": "running"]))
        #expect(p.step == .inspect && p.stillsDone == 1 && p.stillsTotal == 2)
        p.update(view(["select": "done", "inspect": "done", "fuse": "running", "locate": "pending", "report": "pending"]))
        #expect(p.step == .weigh)
        p.update(view(["select": "done", "inspect": "done", "fuse": "done", "locate": "done", "report": "running"]))
        #expect(p.step == .write)
    }
}
