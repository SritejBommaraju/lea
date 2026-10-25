import Foundation

/// The one or two sentences Lea says when a report lands: the verdict, the top problem in plain words, and the
/// first safety note when there is one. Built only from the report's own words (ReportModel), never invented.
enum ReportSpeech {
    static func lines(_ r: ReportModel, caveat: String? = nil) -> String {
        var out: [String] = []
        switch r.verdict {
        case .potentialIssues:
            if let top = r.mostLikely.first {
                out.append("I found likely problems. Most likely, \(problem(top)).")
            } else {
                out.append("I found likely problems.")
            }
        case .looksGood:
            out.append("I didn't see any problems in these photos.")
        case .noProblemsSeen:
            // Not "it's fine": nothing looked wrong in what the photos showed clearly, then what that leaves out.
            // The caveat's first sentence restates the first line in the third person, so speak what follows it.
            out.append("Nothing looked wrong in what I could see clearly.")
            if let c = caveat ?? r.verdictCaveat {
                var said = firstSentence(c)
                if said.lowercased().hasPrefix("nothing looked wrong") {
                    let rest = String(c.trimmingCharacters(in: .whitespacesAndNewlines).dropFirst(said.count))
                        .trimmingCharacters(in: .whitespaces)
                    said = rest.isEmpty ? "" : firstSentence(rest)
                }
                if !said.isEmpty { out.append(said) }
            }
        case .insufficientEvidence:
            if let reason = r.verdictReason.map(firstSentence) {
                out.append("I couldn't tell from these photos. \(reason)")
            } else if let next = r.nextStep {
                out.append("I couldn't tell from these photos. \(firstSentence(next.instruction))")
            } else {
                out.append("I couldn't tell from these photos.")
            }
        case .other(let raw):
            out.append(raw.replacingOccurrences(of: "_", with: " ").capitalizedFirst + ".")
        }
        // Then the first safety note: what the person needs before touching anything.
        if let safety = r.safety.first.map({ firstSentence($0.text) }), !safety.isEmpty {
            out.append("Safety first: \(lowerFirst(safety))")
        }
        return out.joined(separator: " ")
    }

    /// "the housing looks damaged" / "the cover: colour doesn't match".
    static func problem(_ p: ReportModel.Problem) -> String {
        let part = lowerFirst(p.part)
        let issue = lowerFirst(p.issue)
        let joins = ["looks ", "may be ", "partly ", "on the "]
        let phrase = joins.contains(where: issue.hasPrefix) ? "the \(part) \(issue)" : "the \(part): \(issue)"
        guard let place = p.whereWords.map(lowerFirst), !place.isEmpty, place.count <= 60 else { return phrase }
        return "\(phrase), \(place)"
    }

    /// The first sentence, with its full stop.
    static func firstSentence(_ text: String) -> String {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let r = t.range(of: #"[.!?](\s|$)"#, options: .regularExpression) {
            return String(t[..<r.upperBound]).trimmingCharacters(in: .whitespaces)
        }
        return t.isEmpty ? t : t + "."
    }

    /// Lower-cases the first letter unless the word is an acronym ("USB", "LED").
    static func lowerFirst(_ s: String) -> String {
        guard let first = s.first else { return s }
        let second = s.dropFirst().first
        if second.map({ $0.isUppercase }) == true { return s }
        return first.lowercased() + s.dropFirst()
    }
}
