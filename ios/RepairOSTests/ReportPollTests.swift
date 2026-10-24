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
