import Foundation

/// VEL's turn-taking rules (VEL's app/speech.mjs and workers/vel-stt/src/endpoint.rs), ported to Swift.
/// Pure functions: what a short utterance means, Lea's own echo, a quick fitting reaction, thinking sounds that hold
/// the turn, where to resume after an interruption, sentence cuts for speech, and when a pause ends a turn.
nonisolated enum TurnTaking {
    // MARK: Text helpers

    static func norm(_ text: String) -> String {
        let lowered = text.lowercased()
        let cleaned = lowered.replacingOccurrences(of: "[^a-z0-9' ]+", with: " ", options: .regularExpression)
        return cleaned.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
    }

    private static func matches(_ pattern: String, _ text: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    // MARK: Turn helpers

    /// "ok go on", "continue", "got it"; a bare "yes" only when Lea offered to carry on.
    static func isBack(_ text: String, offered: Bool = false) -> Bool {
        let t = norm(text)
        if matches("^(ok(ay)?|alright|all right|cool|great|thanks|thank you|right|yeah|yes|yep|sure|perfect|nice|got it|makes sense|i see)?\\s*,?\\s*(go )?(back( to (the|our) (lesson|repair)| to it| to where we were)?|go on|carry on|keep going|continue|let's continue|move on|resume)( please| now)?$", t) { return true }
        if matches("^(ok(ay)? )?(got it|that makes sense|makes sense|i get it( now)?|understood|i see)( thanks| thank you)?( go on| continue| back)?$", t) { return true }
        return offered && matches("^(ok(ay)?|yes|yeah|yep|sure|please|alright|all right|sounds good|let's go|ok(ay)? sure)( please)?$", t)
    }

    /// "mm-hmm", "yeah", "okay": the person is listening, not taking the turn.
    static func isBackchannel(_ text: String) -> Bool {
        matches("^(mm+|m+ ?hm+|uh ?huh|hmm+|yeah|yep|yes|ok(ay)?|right|sure|cool|nice|wow|oh|ah|i see|got it)$", norm(text))
    }

    /// "stop", "wait": the person wants Lea quiet, not a reply ("go on" resumes).
    static func isStop(_ text: String) -> Bool {
        matches("^(ok(ay)? )?(stop|wait|hold on|pause)( please| a (second|sec|moment))?$", norm(text))
    }

    /// Is what the microphone heard just Lea's own voice coming back? `spoken`: what Lea just said.
    static func isEcho(heard: String, spoken: String) -> Bool {
        let h = norm(heard).split(separator: " ").map(String.init)
        let s = Set(norm(spoken).split(separator: " ").map(String.init))
        guard h.count >= 2, !s.isEmpty else { return false }
        return Double(h.filter(s.contains).count) / Double(h.count) >= 0.75
    }

    enum Kind: String { case back, request, confused, question, statement, none }

    static func turnKind(_ text: String) -> Kind {
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines), t = norm(raw)
        if t.isEmpty { return .none }
        if isBack(t, offered: true) { return .back }
        if matches("\\b(don'?t (get|understand)|confus\\w*|i'?m lost|lost me|makes no sense|not following|i'?m stuck)\\b", t) { return .confused }
        if matches("\\b(simpler|easier|slower|slow down|faster|speed up|again|another way|different way|more detail|in plain|skip|an example|for a kid)\\b", t) { return .request }
        if matches("^(hmm |um |so |okay |ok )?(can|could|would|will) you (please )?(explain|show|teach|walk|go|draw|make|give|try|help|tell|break|check|look|compare|change|send|increase|decrease|widen)\\b", t),
           !matches("\\b(why|how come)\\b", t) { return .request }
        if matches("^(please |hmm |um |so |okay |ok )?(explain|show|teach|tell|walk|draw|give|check|look|compare|change|send|make|increase|decrease|widen)\\b", t) { return .request }
        if matches("\\?\\s*$", raw) || matches("^(wait |sorry |hold on |hmm |um |so |but )*(why|how|what|when|where|who|which|is|are|does|do|did|can|could|would|should|isn'?t|aren'?t|doesn'?t|whats)\\b", t) { return .question }
        return .statement
    }

    // MARK: Reactions and thinking sounds

    static let reactions: [Kind: [String]] = [
        .question: ["Oh, good question.", "Oh, interesting.", "Oh, right.", "Ooh, okay."],
        .request: ["Sure.", "Yeah, sure.", "Of course."],
        .confused: ["Okay.", "No worries."],
        .statement: ["Okay.", "Right."],
    ]

    /// A short reaction for this turn, or nil: never one of the last three, praise once a session, and sometimes
    /// nothing at all (people often just pause, then answer). `r`: a random number in 0..<1.
    static func reaction(for text: String, recent: [String], r: Double = .random(in: 0..<1)) -> String? {
        let kind = turnKind(text)
        guard let pool = reactions[kind] else { return nil }
        if (kind == .question || kind == .statement) && r < 0.2 { return nil }
        let praised = recent.contains { $0.range(of: "good (question|one)", options: [.regularExpression, .caseInsensitive]) != nil }
        let last = Set(recent.suffix(3))
        let ok = pool.filter { !last.contains($0) && !(praised && $0.range(of: "good (question|one)", options: [.regularExpression, .caseInsensitive]) != nil) }
        guard !ok.isEmpty else { return nil }
        return ok[Int(((r * 7919).truncatingRemainder(dividingBy: 1)) * Double(ok.count))]
    }

    /// Sounds that hold the turn while the answer is still coming, by how many this wait has had already.
    static let thinking: [[String]] = [
        ["Hmm, let me check that.", "Okay, let me look.", "Um, let me think.", "Let me see…"],
        ["Okay…", "So…"],
        ["I want to get this right.", "Okay, almost."],
    ]

    static func filler(after last: [String], n: Int, r: Double = .random(in: 0..<1)) -> String? {
        let tier = thinking[min(n, thinking.count - 1)]
        var pool = tier.filter { !last.contains($0) }
        if pool.isEmpty { pool = tier.filter { $0 != last.last } }
        return pool.isEmpty ? nil : pool[Int(r * Double(pool.count)) % pool.count]
    }

    /// Silence (seconds) before the n-th thinking sound: about the longest pause a listener lets pass.
    static func fillQuiet(_ n: Int, r: Double = .random(in: 0..<1)) -> Double { (n > 0 ? 1.15 : 1.05) + r * 0.25 }

    private static let leadIn = "^(?:(?:ah+|oh+|ooh+|huh|okay|ok|sure|so|well|hmm+|um+|uh+|right|alright|all right|yeah|mm+|wow)[,!.…—–-]*\\s+)+"

    /// One way into an answer, never a stack: after a reaction, the answer's own opening interjection is dropped.
    static func stripLead(_ text: String) -> String {
        let rest = text.replacingOccurrences(of: leadIn, with: "", options: [.regularExpression, .caseInsensitive])
        guard rest.count >= 2, rest != text else { return text }
        return rest.prefix(1).uppercased() + rest.dropFirst()
    }

    // MARK: Speech shaping

    /// A reply cut into sentences, so speech starts at the first one; pieces under `min` words join a neighbour.
    static func sentences(_ text: String, min: Int = 4) -> [String] {
        let flat = text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        let regex = try! NSRegularExpression(pattern: "[^.!?…]+(?:[.!?…]+[\"'”’)]*|$)")
        let pieces = regex.matches(in: flat, range: NSRange(flat.startIndex..., in: flat))
            .compactMap { Range($0.range, in: flat).map { flat[$0].trimmingCharacters(in: .whitespaces) } }
            .filter { !$0.isEmpty }
        var out: [String] = []
        for p in pieces {
            let words = p.split(separator: " ").count
            if let last = out.last, words < min || last.split(separator: " ").count < min { out[out.count - 1] = last + " " + p }
            else { out.append(p) }
        }
        return out.isEmpty ? (flat.isEmpty ? [] : [flat]) : out
    }

    /// Units and symbols as a person says them, for the few that appear in repair talk.
    static func speakable(_ text: String) -> String {
        var s = text
        for (pattern, spoken) in [("(\\d)\\s*mm\\b", "$1 millimetres"), ("(\\d)\\s*V\\b", "$1 volts"), ("(\\d)\\s*(Ω|ohms?)", "$1 ohms"),
                                  ("(\\d)\\s*mA\\b", "$1 milliamps"), ("(\\d)\\s*%", "$1 percent"), ("±", " plus or minus "), ("×", " by "), ("≈", " about ")] {
            s = s.replacingOccurrences(of: pattern, with: spoken, options: .regularExpression)
        }
        return s.replacingOccurrences(of: "\\s{2,}", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
    }

    // MARK: Resuming after an interruption

    private static let link = "^(so|and|but|because|which|then|or|while|since|until)$"
    private static let bridges = ["So,", "Anyway,", "", "Okay, so", "Right, so", ""]
    private static let linked = ["", "Right,", "Okay,", ""]

    /// The reply picked back up after the person interrupted: from the start of the clause it broke off (`heard`: share
    /// already spoken, 0...1), with a light link or none, varied by `n`. Little heard: the whole sentence.
    static func resumeLine(_ sentence: String, heard: Double, n: Int) -> (text: String, from: Int) {
        let w = sentence.trimmingCharacters(in: .whitespaces).split(whereSeparator: \.isWhitespace).map(String.init)
        let at = Int((max(0, min(1, heard)) * Double(w.count)).rounded())
        var from = 0
        // a clause of at least five words, never a scrap on its own
        if at >= 3 {
            var i = min(at, w.count - 5)
            while i >= 2 {
                if w[i - 1].range(of: "[,;:—–]$", options: .regularExpression) != nil || w[i].lowercased().range(of: link, options: .regularExpression) != nil { from = i; break }
                i -= 1
            }
        }
        var rest = Array(w[min(from, w.count)...])
        let list = (rest.first.map { $0.lowercased().range(of: link, options: .regularExpression) != nil } ?? false) ? linked : bridges
        let bridge = list[((n % list.count) + list.count) % list.count]
        if !bridge.isEmpty, let first = rest.first, first.range(of: "^I('|$)", options: .regularExpression) == nil {
            rest[0] = first.prefix(1).lowercased() + first.dropFirst()
        }
        return (([bridge] + rest).filter { !$0.isEmpty }.joined(separator: " "), from)
    }

    // MARK: End of turn (vel-stt endpoint.rs)

    enum Hint { case finished, unfinished, unsure }
    /// Silence after speech that ends a turn, by what the words so far say.
    static let draftAt: Double = 0.576, clickHangover: Double = 0.9, unfinishedHangover: Double = 1.8, minSpeech: Double = 0.3

    private static let hanging: Set<String> = ["um", "uh", "erm", "er", "hmm", "mm", "so", "and", "but", "or", "because", "cause", "the", "a", "an", "my",
                                               "your", "their", "our", "than", "also", "just", "really", "very", "always", "maybe", "kinda", "sorta", "basically", "actually"]

    static func turnHint(_ text: String) -> Hint {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard let lastChar = t.last else { return .unsure }
        if ",-–—…:;".contains(lastChar) { return .unfinished }
        let words = t.split(whereSeparator: \.isWhitespace)
        let last = words.last!.lowercased().trimmingCharacters(in: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "'")).inverted)
        if hanging.contains(last) { return .unfinished }
        if words.count >= 3 && "?.!".contains(lastChar) { return .finished }
        return .unsure
    }

    /// How long a pause after these words must last before the turn is over.
    static func hangover(after text: String) -> Double {
        switch turnHint(text) {
        case .finished: draftAt
        case .unfinished: unfinishedHangover
        case .unsure: clickHangover
        }
    }
}
