import Foundation

/// The words the dark-stage report leads with, built only from the report's own fields (never invented, never
/// naming a kind of object in app code): a counts eyebrow, a headline that is the top finding in the backend's
/// words, and one honest sentence. Also cleans backend internals (still ids, snake_case, detector jargon) out of
/// the "couldn't check" rows. Pure, so it is unit-tested (ReportWordsTests).
enum ReportWords {
    // MARK: Verdict

    /// "3 likely problems · 1 safety", "Nothing looked wrong · 12 spots unchecked", "Not enough to call it yet".
    /// The view shows it upper-cased; VoiceOver reads it as written.
    static func eyebrow(_ r: ReportModel) -> String {
        switch r.verdict {
        case .potentialIssues:
            let n = r.mostLikely.count
            var parts = [n == 0 ? "Likely problems" : n == 1 ? "1 likely problem" : "\(n) likely problems"]
            let safety = (r.mostLikely + r.otherIssues).filter { $0.severity == .safety }.count
            if safety > 0 { parts.append("\(safety) safety") }
            return parts.joined(separator: " · ")
        case .looksGood:
            let gaps = unchecked(r)
            let base = gaps > 0 ? "Nothing looked wrong · \(spots(gaps, digits: true)) unchecked" : "Nothing looked wrong"
            return r.setup.isEmpty ? base : "\(base) · \(setupCount(r))"
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
            // Lead with what to do (the next step's title, e.g. "Take a closer photo"); the eyebrow says why.
            return r.nextStep?.title ?? "Lea needs a closer look"
        case .noProblemsSeen:
            if let s = r.setup.first { return s.fix ?? phrase(s) }
            return "No problems seen"
        case .other(let raw):
            return raw.replacingOccurrences(of: "_", with: " ").capitalizedFirst
        }
    }

    /// One sentence under the headline, from fields only: how often the top finding was seen, what else is worth a
    /// look, and what was left unchecked when nothing looked wrong.
    static func sentence(_ r: ReportModel) -> String? {
        var out: [String] = []
        switch r.verdict {
        case .potentialIssues:
            if let top = r.mostLikely.first {
                if top.proved { out.append("Confirmed with a meter.") }
                else if top.regions > 1 { out.append("Seen \(top.regions) times.") }
            }
            let more = max(0, r.mostLikely.count - 1) + r.otherIssues.count
            if more == 1 { out.append("One more thing is worth a look.") }
            else if (2...3).contains(more) { out.append("\(count(more).capitalizedFirst) more things are worth a look.") }
            else if more > 3 { out.append("\(more) more are listed below.") }
        case .looksGood:
            let gaps = unchecked(r)
            if gaps > 0 {
                out.append(gaps == 1 ? "One spot wasn't clear enough to judge, so it isn't counted as fine."
                                     : "\(spots(gaps).capitalizedFirst) weren't clear enough to judge, so they aren't counted as fine.")
            }
        case .insufficientEvidence:
            if let next = r.nextStep { out.append(sanitize(firstSentence(next.instruction))) }
            else if let reason = r.verdictReason { out.append(sanitize(firstSentence(reason))) }
        case .noProblemsSeen:
            // The caveat, as the backend wrote it (one quiet line); else what was left unchecked.
            if let caveat = r.verdictCaveat { out.append(sanitize(caveat)) }
            else if unchecked(r) == 1 { out.append("One spot needs a closer photo.") }
            else if unchecked(r) > 1 { out.append("\(spots(unchecked(r)).capitalizedFirst) need a closer photo.") }
        case .other:
            break
        }
        return out.isEmpty ? nil : out.joined(separator: " ")
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

    /// Removes internals from backend text: still ids ("still scan_ab12_s3", "s11") become "photo 3", snake_case
    /// identifiers become words, and detector-limit jargon becomes plain words.
    static func sanitize(_ text: String) -> String {
        var t = text
        t = t.replacing(/\bin (?:the )?still (?:scan_[0-9a-f]+_)?s(\d+)\b/) { "in photo \($0.1)" }
        t = t.replacing(/\bstill (?:scan_[0-9a-f]+_)?s(\d+)\b/) { "photo \($0.1)" }
        t = t.replacing(/\bscan_[0-9a-f]+_s(\d+)\b/) { "photo \($0.1)" }
        t = t.replacing(/past this photo's limit of the strongest detector regions/) { _ in "too many in this photo" }
        t = t.replacing(/\b([a-z]+)_([a-z_]+)\b/) { "\($0.1) \($0.2.replacingOccurrences(of: "_", with: " "))" }
        return t
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
