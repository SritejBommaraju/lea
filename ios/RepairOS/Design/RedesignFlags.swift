import Foundation

/// Switches for the redesign, so each new piece swaps in with a few lines and can be turned off for a side-by-side.
enum RedesignFlags {
    /// The dark-stage report (Report/Stage): the scan full-bleed on a dark stage, the verdict sheet over it, the
    /// "What Lea couldn't check" summary, markers that never overlap and the landing choreography. On by default;
    /// the `--old-report` launch argument turns it off.
    static let reportStage = !CommandLine.arguments.contains("--old-report")
}
