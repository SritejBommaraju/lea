import Foundation

/// The words of Lea's answers as the screen shows them. The gateway already sends clean, whole sentences
/// (`SentenceSplitter`, `unspeakable` in backend/repairos/voice/conversation.py); this is the phone's own last line of
/// defence, so nothing meant for the machine ever reaches a caption: a marker (`<show part="ix_3"/>`, one cut off at
/// the end of a truncated answer, or the tail of one), an image name ("in photo s2"), a part or scan id, or the
/// punctuation a removed marker leaves behind (":.", " ,"). It also holds the sentence and repeat rules the captions
/// use, so they are tested in one place (LeaTextTests).
enum LeaText {
    /// One sentence (or a stored answer) as it is shown: markers, image names and ids out, tidied. "" when nothing is
    /// left to show.
    static func clean(_ text: String) -> String {
        var s = text.replacingOccurrences(of: "\n", with: " ")
        // Markers: whole ones, the tail of one cut at the start (`part="ix_2"/>`), and one still open at the end.
        s = s.replacing(/<[^<>]*>/, with: " ")
        s = s.replacing(/^[^<>]*?\b\w+\s*=\s*"[^"]*"\s*\/?>/, with: " ")
        s = s.replacing(/<[^>]*$/, with: " ")
        s = s.replacing(/\/>/, with: " ")
        // Image names and ids are never shown.
        s = s.replacing(imageName, with: " ")
        s = s.replacing(identifier, with: " ")
        // Tidy what the removals left.
        s = s.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        s = s.replacing(/\(\s*\)/, with: "")
        s = s.replacing(/\s+([,.;:!?])/) { String($0.1) }
        s = s.replacing(/^[,;:.!?]+\s*/, with: "")
        s = s.replacing(/[,;:]\s*([.!?])/) { String($0.1) }       // ":." and ",." left before a stop
        s = s.replacing(/([,;:])\s*[,;:]+/) { String($0.1) }       // ", ," and ":;"
        s = s.replacing(/([!?])\s*\.(?!\.)/) { String($0.1) }      // "?." → "?"
        s = s.replacing(/\.{2,}/) { $0.output.count == 2 ? "." : "…" }      // ".." → "."; an ellipsis stays one
        s = s.replacing(/\s*[,;:]$/, with: "")
        s = s.trimmingCharacters(in: .whitespaces)
        guard let first = s.first else { return "" }
        // Nothing but punctuation left is nothing.
        guard s.contains(where: { $0.isLetter || $0.isNumber }) else { return "" }
        return first.uppercased() + s.dropFirst()
    }

    /// "in photo s2", "as shown in view v1", ", the image s3," (case-insensitive), with the comma round it.
    nonisolated(unsafe) private static let imageName =
        /(?i)(?:,\s*)?\b(?:(?:as\s+)?(?:shown\s+)?(?:in|on|from|at)\s+)?(?:the\s+)?(?:photo|view|image|picture|still|render)\s+[sv]\d{1,3}\b(?:\s*,)?/
    /// A finding, index, point, scan or report id.
    nonisolated(unsafe) private static let identifier =
        /\b(?:(?:f|ix|pt)_[a-z0-9]{1,16}|scan_[a-z0-9]{4,}(?:_s\d+)?|rep_scan_[a-z0-9]+)\b/

    /// One streamed sentence added to what is shown of an answer so far: its cleaned words, or nil when there is
    /// nothing new to show (only a marker or an id, or a sentence she already said in this answer).
    static func shown(_ raw: String, after said: [String]) -> String? {
        let s = clean(raw)
        return s.isEmpty || repeats(s, in: said) ? nil : s
    }

    /// The same sentence said again (a model re-stating its answer to place markers; a held answer replayed): equal
    /// once case, spacing and punctuation are ignored.
    static func key(_ sentence: String) -> String {
        String(sentence.lowercased().unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.map(Character.init))
    }

    /// Whether `sentence` repeats one already in `said`.
    static func repeats(_ sentence: String, in said: [String]) -> Bool {
        let k = key(sentence)
        return !k.isEmpty && said.contains { key($0) == k }
    }

    /// A stored answer (history, a replayed record) split into sentences the way the gateway splits them: at `.`, `!`
    /// or `?` followed by a space or the end, so "1.5 mm", "e.g. the fuse" and "v2.0" stay whole. Each one cleaned;
    /// empty ones dropped.
    static func sentences(_ text: String) -> [String] {
        let flat = text.replacingOccurrences(of: "\n", with: " ")
        var out: [String] = []
        var current = ""
        var depth = 0   // inside a marker: never cut there (a label may hold a full stop)
        let chars = Array(flat)
        for (i, ch) in chars.enumerated() {
            current.append(ch)
            if ch == "<" { depth += 1 } else if ch == ">" { depth = max(0, depth - 1) }
            guard depth == 0, ".!?".contains(ch) else { continue }
            let next = i + 1 < chars.count ? chars[i + 1] : " "
            guard next.isWhitespace else { continue }
            if isAbbreviation(current) { continue }
            let s = clean(current)
            if !s.isEmpty { out.append(s) }
            current = ""
        }
        let rest = clean(current)
        if !rest.isEmpty { out.append(rest) }
        return out
    }

    /// "e.g.", "i.e.", "approx.", "No.": a full stop that does not end the sentence.
    private static func isAbbreviation(_ text: String) -> Bool {
        let last = text.split(separator: " ").last.map { $0.lowercased() } ?? ""
        return ["e.g.", "i.e.", "approx.", "vs.", "no.", "mr.", "dr.", "fig."].contains(last)
    }
}
