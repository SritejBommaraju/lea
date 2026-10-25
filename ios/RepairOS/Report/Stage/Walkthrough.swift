import Foundation
import simd

/// "Walk me through it": the report as a guided tour, one card per step, each with the line Lea says over it. Built
/// only from the report's own fields (the same words as ReportWords and ReportSpeech, never invented and never a
/// model call), plain first person, short sentences. Pure, so it is unit-tested (WalkthroughTests).
///
/// The order: the object and the verdict; each most-likely problem (the camera flies to its marker when it has a
/// spot on the scan); the other issues, briefly; each setup item with its fix; the safety notes; the next step; a short
/// summary. Empty sections are left out. What could not be checked is not a step (no coverage caveat by default, owner
/// 2026-10-25): it is behind "What wasn't checked", and Lea answers when asked.
enum Walkthrough {
    struct Step: Identifiable, Equatable, Sendable {
        enum Kind: String, Sendable { case intro, problem, others, setup, safety, next, outro }

        var id: String
        var kind: Kind
        /// The small line over the title: "Problem 1 of 3 · Safety".
        var eyebrow: String
        var title: String
        /// What Lea says; the card shows it too (read along, or read alone when she is muted).
        var speech: String
        /// The marker the camera flies to (a finding with a spot on the scan); nil frames the whole object.
        var focus: String?
        /// The evidence photos (crop sha256s), the first shown in the card.
        var crops: [String] = []
        /// The problem's number on the scan (the markers' 1, 2, 3).
        var number: Int?
        var tone: Tone = .accent
    }

    /// Setup items said one by one; the rest are counted.
    static let maxSetup = 3

    /// The tour's steps. `located`: whether a problem has a marker the hero can fly to (the 3D point, or the photo
    /// hero's crop).
    static func steps(_ r: ReportModel, located: (ReportModel.Problem) -> Bool = { $0.point != nil }) -> [Step] {
        var out = [intro(r)]
        let n = r.mostLikely.count
        for (i, p) in r.mostLikely.enumerated() { out.append(problem(p, index: i, of: n, located: located(p))) }
        if let s = others(r.otherIssues + r.cosmetic) { out.append(s) }
        for (i, p) in r.setup.prefix(maxSetup).enumerated() {
            out.append(setup(p, index: i, of: r.setup.count, last: i == min(r.setup.count, maxSetup) - 1))
        }
        if let s = safety(r.safety) { out.append(s) }
        if let s = next(r) { out.append(s) }
        out.append(outro(r))
        return out
    }

    // MARK: Steps

    static func intro(_ r: ReportModel) -> Step {
        let kind = r.objectWords.map { String($0.split(separator: "·").first ?? "").trimmingCharacters(in: .whitespaces) }
        var said = [kind.map { "Let's walk through your \(ReportSpeech.lowerFirst($0))." } ?? "Let's walk through your scan."]
        said.append(verdictLine(r))
        if r.verdict == .potentialIssues {
            let safety = r.mostLikely.filter { $0.severity == .safety }.count
            if safety == 1, r.mostLikely.count > 1 { said.append("One of them is a safety issue.") }
            else if safety == 1 { said.append("It's a safety issue.") }
            else if safety > 1, safety == r.mostLikely.count { said.append("All of them are safety issues.") }
            else if safety > 1 { said.append("\(words(safety).capitalizedFirst) of them are safety issues.") }
        }
        return Step(id: "intro", kind: .intro, eyebrow: ReportWords.eyebrow(r), title: kind ?? "Your scan",
                    speech: said.joined(separator: " "), tone: ReportWords.tone(r))
    }

    /// One most-likely problem: what and where, why (what the photo shows), how sure, and its fix when it has one.
    static func problem(_ p: ReportModel.Problem, index i: Int, of n: Int, located: Bool) -> Step {
        var said = [n == 1 ? "Here's the problem: \(spoken(p))." : "\(ordinal(i)), \(spoken(p))."]
        if let place = whereLine(p.whereWords) { said.append(place) }
        if let obs = p.observation.map({ ReportSpeech.firstSentence(ReportWords.sanitize($0)) }), !obs.isEmpty {
            said.append(obs.hasPrefix("I ") ? obs : "In the photo, \(ReportSpeech.lowerFirst(obs))")
        } else if p.photoLabels.count > 1 {
            said.append("I saw it in \(words(p.photoLabels.count)) photos.")
        }
        if p.regions > 1 { said.append("I saw it \(words(p.regions)) times.") }
        said.append(sureLine(p))
        if let fix = p.fix { said.append(ReportSpeech.firstSentence(ReportWords.sanitize(fix))) }
        var eyebrow = "Problem \(i + 1) of \(n)"
        if let s = p.severity { eyebrow += " · \(s.words)" }
        return Step(id: "problem:\(p.id)", kind: .problem, eyebrow: eyebrow, title: ReportWords.phrase(p),
                    speech: said.joined(separator: " "), focus: located ? p.id : nil, crops: p.crops, number: i + 1,
                    tone: p.severity.tone)
    }

    /// The other issues (lower ranked, cosmetic), briefly: up to two by name, the rest counted.
    static func others(_ list: [ReportModel.Problem]) -> Step? {
        guard !list.isEmpty else { return nil }
        let named = list.prefix(2).map(spoken)
        var line: String
        if list.count == 1 {
            line = "One more thing is worth a look: \(named[0])."
        } else {
            line = "\(words(list.count).capitalizedFirst) more things are worth a look: \(named.joined(separator: ", and "))"
            line += list.count > 2 ? ", and \(words(list.count - 2)) more in the report." : "."
        }
        return Step(id: "others", kind: .others, eyebrow: list.count == 1 ? "1 more" : "\(list.count) more",
                    title: "Also worth a look", speech: line, crops: list.first?.crops ?? [], tone: .caution)
    }

    /// A setup or assembly state: never called broken, said with its fix.
    static func setup(_ p: ReportModel.Problem, index i: Int, of n: Int, last: Bool) -> Step {
        var said = ["\(i == 0 ? "Something" : "Also something") to set up, not broken: \(spoken(p))."]
        if let fix = p.fix { said.append(ReportSpeech.firstSentence(ReportWords.sanitize(fix))) }
        if last, n > maxSetup { said.append("There \(n - maxSetup == 1 ? "is one more" : "are \(words(n - maxSetup)) more") in the report.") }
        return Step(id: "setup:\(p.id)", kind: .setup, eyebrow: n == 1 ? "To set up" : "To set up · \(i + 1) of \(n)",
                    title: p.fix.map { ReportSpeech.firstSentence(ReportWords.sanitize($0)) } ?? ReportWords.phrase(p),
                    speech: said.joined(separator: " "), crops: p.crops, tone: .accent)
    }

    /// The safety notes, first sentence of each (at most two).
    static func safety(_ notes: [ReportModel.SafetyNote]) -> Step? {
        guard !notes.isEmpty else { return nil }
        let lines = notes.prefix(2).map { ReportSpeech.firstSentence(ReportWords.sanitize($0.text)) }.filter { !$0.isEmpty }
        guard let first = lines.first else { return nil }
        var said = ["Safety first: \(ReportSpeech.lowerFirst(first))"]
        if lines.count > 1 { said.append("Also, \(ReportSpeech.lowerFirst(lines[1]))") }
        return Step(id: "safety", kind: .safety, eyebrow: "Safety", title: ReportWords.safetyLine(notes) ?? "Safety first",
                    speech: said.joined(separator: " "), tone: .danger)
    }

    /// The report's next step, and where to buy the part when the parts search found it.
    static func next(_ r: ReportModel) -> Step? {
        guard let step = r.nextStep else { return nil }
        var said = ["Here's what to do next. \(ReportSpeech.firstSentence(ReportWords.sanitize(step.instruction)))"]
        if let fix = r.fix { said.append("I also found where to get a new \(ReportSpeech.lowerFirst(fix.part)). It's in the report.") }
        return Step(id: "next", kind: .next, eyebrow: "Next step", title: specificTitle(step) ?? first(step.instruction),
                    speech: said.joined(separator: " "), tone: .accent)
    }

    /// The short summary the tour ends on.
    static func outro(_ r: ReportModel) -> Step {
        var said: [String] = []
        switch r.verdict {
        case .potentialIssues:
            if let top = r.mostLikely.first ?? r.otherIssues.first { said.append("To sum up, most likely \(spoken(top)).") }
            else { said.append("To sum up, I found likely problems.") }
        case .looksGood: said.append("To sum up, I didn't see any problems in these photos.")
        case .noProblemsSeen: said.append("To sum up, nothing looked wrong in what I could see clearly.")
        case .insufficientEvidence: said.append("To sum up, I couldn't tell from these photos.")
        case .other: said.append("To sum up: \(r.verdict.words.lowercased()).")
        }
        if !r.setup.isEmpty { said.append(r.setup.count == 1 ? "One thing needs setting up." : "\(words(r.setup.count).capitalizedFirst) things need setting up.") }
        if let step = r.nextStep {
            said.append(specificTitle(step).map { "Start with the next step: \(ReportSpeech.lowerFirst($0))." } ?? "Start with the next step.")
        }
        said.append("That's the walkthrough.")
        return Step(id: "outro", kind: .outro, eyebrow: "Summary", title: "In short", speech: said.joined(separator: " "),
                    tone: ReportWords.tone(r))
    }

    // MARK: Words

    /// The next step's own title ("Take a closer photo"); nil for a kind with only the generic "Next step".
    static func specificTitle(_ step: ReportModel.NextStep) -> String? {
        step.title == "Next step" ? nil : step.title
    }

    private static func first(_ text: String) -> String { ReportSpeech.firstSentence(ReportWords.sanitize(text)) }

    /// The verdict in one first-person sentence (as ReportSpeech says it when a report lands).
    static func verdictLine(_ r: ReportModel) -> String {
        switch r.verdict {
        case .potentialIssues:
            let n = r.mostLikely.count
            return n == 0 ? "I found likely problems." : n == 1 ? "I found one likely problem." : "I found \(words(n)) likely problems."
        case .looksGood: return "I didn't see any problems in these photos."
        case .noProblemsSeen: return "Nothing looked wrong in what I could see clearly."
        case .insufficientEvidence:
            let why = r.verdictReason.map { ReportSpeech.firstSentence(ReportWords.sanitize($0)) }
            return "I couldn't tell from these photos." + (why.map { " " + $0 } ?? "")
        case .other: return r.verdict.words + "."
        }
    }

    /// "the wire may be loose", "the cover: colour doesn't match" (no place: that is its own sentence).
    static func spoken(_ p: ReportModel.Problem) -> String {
        var q = p
        q.whereWords = nil
        return ReportSpeech.problem(q)
    }

    /// Where it is, as a sentence: "It's near the left corner." / "Look at the red wire above the motor."
    static func whereLine(_ words: String?) -> String? {
        guard let raw = words.map({ ReportWords.sanitize($0).trimmingCharacters(in: .whitespacesAndNewlines) }), !raw.isEmpty
        else { return nil }
        let place = ReportSpeech.lowerFirst(raw).trimmingCharacters(in: CharacterSet(charactersIn: "."))
        let first = place.split(separator: " ").first.map(String.init) ?? ""
        let prepositions: Set = ["near", "on", "at", "in", "under", "above", "below", "behind", "by", "next", "inside", "beside",
                                 "between", "along", "around", "over", "underneath", "beneath", "towards", "toward", "where"]
        if prepositions.contains(first) { return "It's \(place)." }
        if ["the", "a", "an", "this", "its", "one"].contains(first) { return "Look at \(place)." }
        return "Look at the \(place)."
    }

    /// How sure: a meter reading proves it (or rules it out); otherwise Jev's likelihood that this is the fault, else
    /// the vision confidence, and always that photos alone are not proof.
    static func sureLine(_ p: ReportModel.Problem) -> String {
        if p.ruledOut { return "A meter reading ruled it out, so it isn't the problem." }
        if p.proved { return "A meter reading confirmed it." }
        let sure: String? = switch p.likelihood?.lowercased() {
        case "very likely": "I think it's very likely the cause."
        case "likely": "I think it's likely the cause."
        case "unsure": "I'm not sure it's the cause."
        case "unlikely": "I think it's unlikely to be the cause."
        case "very unlikely": "I think it's very unlikely to be the cause."
        default:
            switch p.confidence?.lowercased().replacingOccurrences(of: " confidence", with: "") {
            case "high": "I'm fairly sure about this one."
            case "medium": "I'm only partly sure about this one."
            case "low": "I'm not very sure about this one."
            default: nil
            }
        }
        return [sure, "It's from photos only, so it isn't proven."].compactMap { $0 }.joined(separator: " ")
    }

    private static func ordinal(_ i: Int) -> String {
        let words = ["First", "Second", "Third", "Fourth", "Fifth"]
        return i < words.count ? words[i] : "Next"
    }

    /// Small counts as words ("two"), larger ones as digits.
    static func words(_ n: Int) -> String {
        let w = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
        return n < w.count ? w[n] : "\(n)"
    }

    // MARK: Voice "stop"

    /// The words that end the tour by voice.
    static let stopPhrases: Set = ["stop", "stop it", "stop please", "please stop", "stop the tour", "stop the walkthrough",
                                   "end the tour", "end the walkthrough", "that's enough", "thats enough", "enough", "cancel"]
    private static let names: Set = ["lea", "leah", "lia", "leia", "leya", "lee"]
    private static let greetings: Set = ["hey", "hi", "hay", "ok", "okay"]

    /// Whether what the phone heard ends the tour. While Lea is speaking the microphone also hears her, so only her
    /// name right before a stop phrase counts ("Lea, stop", "Hey Lea, that's enough"): she never says her own name.
    /// When she is quiet, the stop phrase alone does ("Stop").
    static func isStop(_ heard: String, leaSpeaking: Bool) -> Bool {
        let w = heard.lowercased().map { $0.isLetter || $0 == "'" ? $0 : " " }.reduce(into: "") { $0.append($1) }
            .split(separator: " ").map(String.init)
        guard !w.isEmpty else { return false }
        if let at = w.lastIndex(where: names.contains) {
            if stopPhrases.contains(w[(at + 1)...].joined(separator: " ")) { return true }   // "Lea, stop"
            if at == w.count - 1, at >= 1 {                                                    // "Stop, Lea"
                for k in 1...min(3, at) where stopPhrases.contains(w[(at - k)..<at].joined(separator: " ")) { return true }
            }
        }
        guard !leaSpeaking else { return false }
        var rest = w[...]
        while let first = rest.first, greetings.contains(first) || names.contains(first) { rest = rest.dropFirst() }
        return stopPhrases.contains(rest.joined(separator: " "))
    }
}

// MARK: - Camera

/// Where the camera is and what it looks at, in the hero's world frame.
struct TourPose: Equatable, Sendable {
    var eye: SIMD3<Float>
    var look: SIMD3<Float>
}

/// What the hero needs from the tour on every frame: the marker of each step (nil: the whole object) and the pager's
/// continuous position (1.5 is halfway between the second and third cards). `cut`: Reduce Motion (no flight).
struct TourFrame: Equatable, Sendable {
    var focuses: [String?]
    var progress: Double
    var cut = false

    /// The step the pager is nearest to.
    var nearest: Int { focuses.isEmpty ? 0 : min(focuses.count - 1, max(0, Int(progress.rounded()))) }
}

/// The tour's camera: one continuous path from step to step, driven by the pager's position (a partial swipe moves
/// the camera partway). Between two views the camera swings round the object (azimuth, elevation and distance from
/// its centre are blended, the short way round) instead of cutting through it, and the look-at point glides from
/// one marker to the next. Pure simd, so it is unit-tested.
enum TourCamera {
    /// `a` → `b` at `t` (0...1), eased (smoothstep) so it leaves and lands gently while the card settles.
    static func blend(_ a: TourPose, _ b: TourPose, _ t: Float, around c: SIMD3<Float>) -> TourPose {
        let t = min(1, max(0, t))
        let e = t * t * (3 - 2 * t)
        let (ra, aza, ela) = spherical(a.eye - c)
        let (rb, azb, elb) = spherical(b.eye - c)
        var d = azb - aza
        if d > .pi { d -= 2 * .pi } else if d < -.pi { d += 2 * .pi }
        // Distance blended on a log scale, so flying in close feels as even as pulling out.
        let la = Foundation.log(max(ra, 1e-4)), lb = Foundation.log(max(rb, 1e-4))
        let r = exp(la + (lb - la) * e)
        let az = aza + d * e, el = ela + (elb - ela) * e
        let eye = c + r * SIMD3(cos(el) * sin(az), sin(el), cos(el) * cos(az))
        return TourPose(eye: eye, look: simd_mix(a.look, b.look, SIMD3(repeating: e)))
    }

    /// The pose at `progress` along `poses` (one per step). `cut`: the nearest step's pose, no blend.
    static func pose(_ poses: [TourPose], at progress: Double, around c: SIMD3<Float>, cut: Bool = false) -> TourPose? {
        guard !poses.isEmpty else { return nil }
        let p = min(Double(poses.count - 1), max(0, progress))
        if cut { return poses[Int(p.rounded())] }
        let i = Int(p.rounded(.down))
        guard i + 1 < poses.count, p - Double(i) > 1e-6 else { return poses[i] }
        return blend(poses[i], poses[i + 1], Float(p - Double(i)), around: c)
    }

    /// How lit each marker is (0...1): 1 on its own card, fading out as the pager leaves it, so the next ring fades in
    /// as the camera arrives and the last one fades out.
    static func weights(_ focuses: [String?], at progress: Double, cut: Bool = false) -> [String: Float] {
        var out: [String: Float] = [:]
        for (i, f) in focuses.enumerated() {
            guard let f else { continue }
            let w: Float = cut ? (Int(progress.rounded()) == i ? 1 : 0) : Float(max(0, 1 - abs(progress - Double(i))))
            out[f] = max(out[f] ?? 0, w)
        }
        return out
    }

    /// (radius, azimuth about +Y from +Z, elevation).
    static func spherical(_ v: SIMD3<Float>) -> (Float, Float, Float) {
        let r = max(simd_length(v), 1e-5)
        return (r, atan2(v.x, v.z), asin(min(1, max(-1, v.y / r))))
    }
}
