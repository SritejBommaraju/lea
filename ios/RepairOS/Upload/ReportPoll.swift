import Foundation

/// What the report screen does after each `GET /v1/scans/{id}/pipeline`, as a pure function of the revision it is
/// showing and what the gateway says. `GET /report` always serves the latest finished report (200, `X-Report-Rev`),
/// so a report on screen is never taken away while a newer one is built.
enum ReportPoll {
    enum Action: Equatable {
        /// Ask again shortly; `updating` shows the "Updating…" chip over the current report.
        case wait(updating: Bool)
        /// A newer finished report exists: fetch it (and keep polling if more is coming).
        case fetch
        /// Nothing newer is coming: the report on screen stands.
        case settle
        /// No report at all and the run failed.
        case fail(String)
    }

    /// - Parameters:
    ///   - showingRev: the revision on screen (nil: none yet; 0: a cached report of unknown revision).
    ///   - status: pipeline status (`not_started`, `inspecting`, … `done`, `closed`, `failed`).
    ///   - reportRev: the latest finished revision (nil before the first report, or from an older gateway).
    ///   - rebuilding: a newer report is pending (new stills, a re-posted scan).
    static func decide(showingRev: Int?, status: String?, reportRev: Int?, rebuilding: Bool, error: String? = nil) -> Action {
        if let latest = reportRev, latest > (showingRev ?? 0) { return .fetch }
        if showingRev == nil, reportRev == nil, status == "done", !rebuilding { return .fetch }   // gateway without report_rev
        if rebuilding { return .wait(updating: showingRev != nil) }
        switch status {
        case "done", "closed":
            return showingRev == nil ? .wait(updating: false) : .settle
        case "failed":
            return showingRev == nil ? .fail(error ?? "unknown error") : .settle
        default:
            return .wait(updating: showingRev != nil)
        }
    }
}
