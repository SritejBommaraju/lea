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

/// What the person is told while the report is on its way, as a pure function of where the scan is and how long Grain
/// has had it. Three honest states instead of a spinner: still sending, "Uploaded. Grain is looking at it." and, once
/// it has been longer than usual, "Taking longer than usual" (with the minutes); a failure says why and offers a retry.
struct ReportArrival: Equatable {
    /// After this long the line admits it is taking longer than usual.
    static let slowAfter: TimeInterval = 5 * 60

    enum Phase: Equatable { case sending, looking, slow, failed }

    var phase: Phase
    var title: String
    var detail: String?

    static func state(step: PipelineProgress.Step, uploaded: Int, pending: Int, waited: TimeInterval,
                      error: String?) -> ReportArrival {
        if let error {
            return ReportArrival(phase: .failed, title: "Something went wrong", detail: error)
        }
        if step == .upload {
            let total = uploaded + pending
            return ReportArrival(phase: .sending, title: total > 0 ? "Sending your scan: \(min(uploaded + 1, total)) of \(total)" : "Sending your scan",
                                 detail: nil)
        }
        let leave = "Keep this screen open until it is ready."
        if waited >= slowAfter {
            let minutes = Int(waited / 60)
            return ReportArrival(phase: .slow, title: "Taking longer than usual",
                                 detail: "Grain has had your scan for \(minutes) minutes and is still working. \(leave)")
        }
        return ReportArrival(phase: .looking, title: "Uploaded. Grain is looking at it.",
                             detail: "This usually takes a few minutes. \(leave)")
    }
}
