import Foundation

/// Which scan cues Lea says out loud, and in what words. Only the cues that change what the person does next are
/// spoken; the steady ones ("Walk slowly around it") stay on screen only. The words are the capture flow's own
/// generic words (no kind of part is ever named).
enum VoiceCues {
    /// On-screen cue text → the short line Lea says. Cue texts come from ScanSession, QualityGate and ScanView.
    static let spoken: [String: String] = [
        "Move closer here": "Move closer here.",
        "Move closer": "Move closer.",
        "Other side": "Now the other side.",
        "Keep the object in frame": "Keep it in frame.",
        "Too close: back up": "Back up a little.",
        "Too dark: add light": "Add some light.",
        "Add light or move closer": "Add some light.",
        "Tap the object to focus on it": "Tap the object.",
        "All sides covered": "All sides covered. Tap Done.",
        "Scan saved": "Got it.",
    ]

    /// The line for a cue, or nil when that cue is not worth saying.
    static func line(for cue: String?) -> String? {
        guard let cue else { return nil }
        // A cue with a detail after the colon ("Move closer: about 22 cm away") is said by its first part.
        let text = cue.trimmingCharacters(in: .whitespacesAndNewlines)
        return spoken[text] ?? text.split(separator: ":", maxSplits: 1).first.flatMap { spoken[String($0)] }
    }

    /// Every fixed line, made once at app start so a cue plays at once.
    static var fixedLines: [String] { Array(Set(spoken.values)).sorted() }
}

/// The scan-cue pacing rule: at most one utterance every `minGap` seconds, never over another utterance, and never
/// the same line twice within `repeatWindow` seconds. Pure (time is passed in) so it is unit-tested.
struct CueLimiter: Sendable {
    enum Decision: Equatable, Sendable {
        case speak
        /// Try again at this time (the gap since the last line is not over, or something is still playing).
        case wait(until: TimeInterval)
        /// Not now and not later: the same line was said too recently.
        case drop
    }

    var minGap: TimeInterval = 4
    var repeatWindow: TimeInterval = 10
    /// How often to look again while another utterance is playing.
    var busyRetry: TimeInterval = 0.5
    private(set) var lastStart: TimeInterval = -.infinity
    private(set) var lastSaid: [String: TimeInterval] = [:]

    /// `held`: a line that must be heard is waiting (what the object is), and this one goes after it.
    func decide(_ line: String, now: TimeInterval, busy: Bool, held: Bool = false) -> Decision {
        if let t = lastSaid[line], now - t < repeatWindow { return .drop }
        if now - lastStart < minGap { return .wait(until: lastStart + minGap) }
        if busy || held { return .wait(until: now + busyRetry) }
        return .speak
    }

    /// Record that `line` started playing at `now` (any utterance counts for the gap, not only cues).
    mutating func spoke(_ line: String?, at now: TimeInterval) {
        lastStart = now
        if let line { lastSaid[line] = now }
        lastSaid = lastSaid.filter { now - $0.value < repeatWindow }
    }
}
