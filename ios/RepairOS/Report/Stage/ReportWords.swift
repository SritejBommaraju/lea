import Foundation

/// The words the dark-stage report leads with, built only from the report's own fields (never invented, never
/// naming a kind of object in app code): a counts eyebrow, a headline that is the top finding in the backend's
/// words, and one honest sentence. Also cleans backend internals (still ids, snake_case, detector jargon) out of
/// the "couldn't check" rows. Pure, so it is unit-tested (ReportWordsTests).
enum ReportWords {
    // MARK: Verdict

    /// "3 likely problems · 1 safety", "Nothing looked wrong", "Not enough to call it yet". Not shown on screen (an
    /// eyebrow over a heading is banned, DESIGN §12): VoiceOver's landing announcement and the walkthrough read it.
    static func eyebrow(_ r: ReportModel) -> String {
        switch r.verdict {
        case .potentialIssues:
            let n = r.mostLikely.count
            var parts = [n == 0 ? "Likely problems" : n == 1 ? "1 likely problem" : "\(n) likely problems"]
            let safety = (r.mostLikely + r.otherIssues).filter { $0.severity == .safety }.count
            if safety > 0 { parts.append("\(safety) safety") }
            return parts.joined(separator: " · ")
        case .looksGood:
            // No coverage caveat by default (owner, 2026-10-25): what wasn't checked is one tap away, never counted as fine.
            return r.setup.isEmpty ? "Nothing looked wrong" : "Nothing looked wrong · \(setupCount(r))"
        case .insufficientEvidence:
            return "Not enough to call it yet"
        case .noProblemsSeen:
            let base = r.photosInspected > 0 ? "\(r.photosInspected) photos checked" : "Checked"
            return r.setup.isEmpty ? base : "\(base) · \(setupCount(r))"
        case .other(let raw):
            return raw.replacingOccurrences(of: "_", with: " ").capitalizedFirst
        }
    }

    /// "1 thing to set up", "2 things to set up": setup and assembly states (report `setup`), never called broken.
    static func setupCount(_ r: ReportModel) -> String {
        r.setup.count == 1 ? "1 thing to set up" : "\(r.setup.count) things to set up"
    }

    /// Under "Setup to check": what these rows are, and are not.
    static let setupNote = "Not broken: put these right, then test again."

    /// A setup row read aloud: its number (as on its marker), the part, how it looks, then its fix.
    static func setupLabel(_ p: ReportModel.Problem, number: Int) -> String {
        "\(number): \(p.part), \(p.issue). " + (p.fix.map(sanitize) ?? "Set up, not broken.")
    }

    /// The top finding in the backend's own words ("Wire may be loose"), or the plain verdict when there is none.
    static func headline(_ r: ReportModel) -> String {
        switch r.verdict {
        case .potentialIssues:
            if let top = r.mostLikely.first ?? r.otherIssues.first { return phrase(top) }
            return "Lea found likely problems"
        case .looksGood:
            if let s = r.setup.first { return s.fix ?? phrase(s) }   // nothing broken: lead with what to put right
            return r.photosInspected > 0 ? "Nothing looked wrong in \(r.photosInspected) photos" : "Nothing looked wrong"
        case .insufficientEvidence:
            // Lead with what to do when it is an action (a meter check); a request for more photos never leads (the
            // owner, 2026-10-25: no "closer photo" prompts in the default report).
            if let step = r.nextStep, !isCaptureRequest(step) { return step.title }
            return "Not enough to call it yet"
        case .noProblemsSeen:
            if let s = r.setup.first { return s.fix ?? phrase(s) }
            return "No problems seen"
        case .other(let raw):
            return raw.replacingOccurrences(of: "_", with: " ").capitalizedFirst
        }
    }

    /// One sentence under the headline, from fields only: the counts (how many likely problems, how many of them are
    /// safety problems), how sure the top one is, and what else is worth a look. No coverage caveat by default (owner,
    /// 2026-10-25): what wasn't checked lives behind "What wasn't checked" and is never called fine.
    static func sentence(_ r: ReportModel) -> String? {
        var out: [String] = []
        switch r.verdict {
        case .potentialIssues:
            if let counts = counts(r) { out.append(counts) }
            if let top = r.mostLikely.first {
                let first = r.mostLikely.count > 1
                if top.proved { out.append(first ? "The first was confirmed with a meter." : "Confirmed with a meter.") }
                else if top.regions > 1 { out.append(first ? "The first was seen \(top.regions) times." : "Seen \(top.regions) times.") }
            }
            let more = r.mostLikely.isEmpty ? max(0, r.otherIssues.count - 1) : r.otherIssues.count
            if more == 1 { out.append("One more thing is worth a look.") }
            else if (2...3).contains(more) { out.append("\(count(more).capitalizedFirst) more things are worth a look.") }
            else if more > 3 { out.append("\(more) more are listed below.") }
        case .looksGood, .noProblemsSeen:
            if !r.setup.isEmpty { out.append(setupCount(r).capitalizedFirst + ".") }
        case .insufficientEvidence:
            if let next = r.nextStep { out.append(sanitize(firstSentence(next.instruction))) }
            else if let reason = r.verdictReason { out.append(sanitize(firstSentence(reason))) }
        case .other:
            break
        }
        return out.isEmpty ? nil : out.joined(separator: " ")
    }

    /// "Two likely problems, one of them a safety problem.", "One likely problem.", nil without a counted problem.
    static func counts(_ r: ReportModel) -> String? {
        let n = r.mostLikely.count
        guard n > 0 else { return nil }
        let safety = r.mostLikely.filter { $0.severity == .safety }.count
        if n == 1 { return safety == 1 ? "One likely problem, a safety problem." : "One likely problem." }
        let base = "\(count(n).capitalizedFirst) likely problems"
        switch safety {
        case 0: return base + "."
        case n: return base + ", all of them safety problems."
        case 1: return base + ", one of them a safety problem."
        default: return base + ", \(count(safety)) of them safety problems."
        }
    }

    /// The tone of the verdict: danger when a likely problem is a safety one, caution for other problems.
    static func tone(_ r: ReportModel) -> Tone {
        switch r.verdict {
        case .potentialIssues: r.mostLikely.contains { $0.severity == .safety } ? .danger : .caution
        case .looksGood, .noProblemsSeen: .good
        default: .cosmetic
        }
    }

    /// "Wire may be loose", "Housing looks cracked", "Cover: colour doesn't match".
    static func phrase(_ p: ReportModel.Problem) -> String {
        let issue = lowerFirst(p.issue)
        let joins = ["looks ", "may be ", "partly ", "on the ", "out of ", "unexpected "]
        let text = joins.contains(where: issue.hasPrefix) ? "\(p.part) \(issue)" : "\(p.part): \(issue)"
        return text.capitalizedFirst
    }

    /// The first safety note's first sentence, as the one line shown above the problems.
    /// "This device looks like it runs on wall power: keep it unplugged while it is open, and remember …" becomes
    /// "Keep it unplugged while it is open." (the instruction after a colon, up to the first ", and" / ";").
    /// Every note stays in full behind "Why".
    static func safetyLine(_ notes: [ReportModel.SafetyNote]) -> String? {
        guard let text = notes.first.map({ firstSentence(sanitize($0.text)) }) else { return nil }
        var line = Substring(text)
        if let colon = line.firstIndex(of: ":") { line = line[line.index(after: colon)...] }
        for stop in [", and ", "; ", " because ", " so "] {
            if let r = line.range(of: stop) { line = line[..<r.lowerBound] }
        }
        var out = line.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: ".,;"))
        guard out.count >= 12 else { return text }
        out = out.capitalizedFirst + "."
        return out
    }

    // MARK: Couldn't check

    /// Not inspected + could not judge, counted once.
    static func unchecked(_ r: ReportModel) -> Int { r.notInspected.count + r.insufficient.count }

    /// "12 spots weren't clear enough and 7 weren't looked at. They aren't counted as fine."
    static func gapSummary(notInspected: Int, unclear: Int) -> String {
        var parts: [String] = []
        if unclear > 0 { parts.append("\(spots(unclear)) \(unclear == 1 ? "wasn't" : "weren't") clear enough") }
        if notInspected > 0 {
            let subject = unclear > 0 ? count(notInspected) : spots(notInspected)
            parts.append("\(subject) \(notInspected == 1 ? "wasn't" : "weren't") looked at")
        }
        guard !parts.isEmpty else { return "" }
        let sentence = parts.joined(separator: " and ").capitalizedFirst
        return "\(sentence). \(unclear + notInspected == 1 ? "It isn't" : "They aren't") counted as fine."
    }

    /// The short reason for one row: "Too small in photo 3", "Not clear enough in photo 11", "Not looked at yet".
    static func gapReason(_ g: ReportModel.Gap, unclear: Bool) -> String {
        let raw = g.reason.lowercased()
        let photo = photoNumber(in: g.reason) ?? g.photoLabel.flatMap(photoNumber(in:))
        let at = photo.map { " in photo \($0)" } ?? ""
        if raw.contains("too small") { return "Too small" + at }
        if raw.contains("blur") { return "Too blurry" + at }
        if raw.contains("dark") { return "Too dark" + at }
        if raw.contains("limit") || raw.contains("not inspected") { return "Not looked at yet" + at }
        if unclear || raw.contains("could not judge") { return "Not clear enough" + at }
        return firstClause(sanitize(g.reason)).capitalizedFirst
    }

    /// A row title without identifiers: "Unknown part" becomes "An area"; snake_case becomes words.
    static func gapTitle(_ g: ReportModel.Gap) -> String {
        let t = sanitize(g.part)
        let generic = ["unknown part", "region", "regions", "small regions", "regions in this photo", "more regions", "unknown"]
        return generic.contains(t.lowercased()) ? "An area" : t.capitalizedFirst
    }

    // MARK: Cleaning backend text

    /// Removes internals from backend text: still ids ("still scan_ab12_s3", "s11") become "photo 3", a detector
    /// region ("the part s1/det0") becomes "the part", snake_case
    /// identifiers become words, and detector-limit jargon becomes plain words.
    static func sanitize(_ text: String) -> String {
        var t = text
        t = t.replacing(/\b(?:the part )?(?:scan_[0-9a-f]+_)?s\d+\/det\d+\b/) { _ in "the part" }
        t = t.replacing(/\bin (?:the )?still (?:scan_[0-9a-f]+_)?s(\d+)\b/) { "in photo \($0.1)" }
        t = t.replacing(/\bstill (?:scan_[0-9a-f]+_)?s(\d+)\b/) { "photo \($0.1)" }
        t = t.replacing(/\bscan_[0-9a-f]+_s(\d+)\b/) { "photo \($0.1)" }
        t = t.replacing(/past this photo's limit of the strongest detector regions/) { _ in "too many in this photo" }
        t = t.replacing(/\b([a-z]+)_([a-z_]+)\b/) { "\($0.1) \($0.2.replacingOccurrences(of: "_", with: " "))" }
        return t
    }

    /// Backend text as the screen shows it: `sanitize`, then no photo numbers at all (the person never sees numbered
    /// photos, so "(photo 8)" or "in photo 3" means nothing to them): a parenthetical photo reference goes, "in photo
    /// 3" goes, and any other "photo 3" becomes "a photo".
    static func display(_ text: String) -> String {
        var t = sanitize(text)
        t = t.replacing(/\s*\((?:see |in |from )?[Pp]hotos? \d+(?:(?:, | and |-)\d+)*\)/) { _ in "" }
        t = t.replacing(/\s+(?:in|from|on) [Pp]hotos? \d+(?:(?:, | and |-)\d+)*/) { _ in "" }
        t = t.replacing(/\b[Pp]hotos? \d+(?:(?:, | and |-)\d+)*\b/) { m in m.output.first == "P" ? "A photo" : "a photo" }
        t = t.replacing(/\ +([.,;:])/) { "\($0.1)" }
        return t
    }

    /// The summary as the report shows it by default: sentences that are coverage caveats ("not a clean bill of
    /// health", "needs a closer photo", "not counted as fine") are left out (owner, 2026-10-25: no caveats by
    /// default; what wasn't checked is behind the ⋯ menu). Everything else stays as the backend wrote it.
    static func withoutCaveats(_ text: String) -> String {
        let caveats = ["clean bill of health", "closer photo", "counted as fine", "could not be checked", "couldn't be checked",
                       "weren't checked", "were not checked", "could not be judged", "couldn't be judged"]
        var sentences: [String] = []
        var current = ""
        for ch in text {
            current.append(ch)
            if ".!?".contains(ch) { sentences.append(current); current = "" }
        }
        if !current.trimmingCharacters(in: .whitespaces).isEmpty { sentences.append(current) }
        let kept = sentences.filter { s in !caveats.contains { s.lowercased().contains($0) } }
        return kept.map { $0.trimmingCharacters(in: .whitespaces) }.joined(separator: " ")
    }

    /// Whether the next step asks for more capture (a closer photo, another angle, a rescan): shown as one short line
    /// at the end of the report, never leading it (owner, 2026-10-25: no caveats first).
    static func isCaptureRequest(_ step: ReportModel.NextStep) -> Bool {
        ["request_closeup", "request_other_angle", "rescan"].contains(step.kind)
    }

    /// The photo number in "s8", "scan_ab12_s8", "Photo 8" or "photo 8", if any.
    static func photoNumber(in text: String) -> Int? {
        if let m = text.firstMatch(of: /(?:^|_|\b)s(\d+)\b/) { return Int(m.1) }
        if let m = text.firstMatch(of: /[Pp]hoto (\d+)\b/) { return Int(m.1) }
        return nil
    }

    // MARK: Helpers

    static func firstSentence(_ text: String) -> String {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let r = t.range(of: #"[.!?](\s|$)"#, options: .regularExpression) {
            return String(t[..<r.upperBound]).trimmingCharacters(in: .whitespaces)
        }
        return t.isEmpty ? t : t + "."
    }

    private static func firstClause(_ text: String) -> String {
        String(text.split(separator: ";", maxSplits: 1).first ?? "").trimmingCharacters(in: .whitespaces)
    }

    private static func lowerFirst(_ s: String) -> String {
        guard let first = s.first else { return s }
        if s.dropFirst().first.map({ $0.isUppercase }) == true { return s }   // an acronym stays as it is
        return first.lowercased() + s.dropFirst()
    }

    /// "one spot", "12 spots"; `digits` for the eyebrow ("12 spots", "3 spots").
    private static func spots(_ n: Int, digits: Bool = false) -> String {
        "\(digits ? "\(n)" : count(n)) \(n == 1 ? "spot" : "spots")"
    }

    /// Small counts as words in sentences ("Two more things"); larger ones as digits.
    private static func count(_ n: Int) -> String {
        let words = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
        return n < words.count ? words[n] : "\(n)"
    }
}
