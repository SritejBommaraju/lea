import Foundation

/// The fix for one report row as data (report `findings[].fix_plan`, spec/contracts/report.schema.json): what to do,
/// where to buy it, which part file to print, or which meter check settles it. Read leniently from the report's
/// JSON, like the rest of ReportModel. Every word about the fix is the backend's; the app only lays it out.
struct FixPlan: Equatable, Sendable {
    enum Action: String, Sendable { case buy, remake, reseat, rewire, check }

    var action: Action
    /// The action in one plain line.
    var summary: String?
    /// Short steps, in order (reseat, rewire).
    var steps: [String]
    /// Where to buy it (action buy).
    var offers: [FixIt.Option]
    /// The part file formats the gateway can serve (action remake): `stl`, `step`.
    var fileFormats: [String]
    /// The next step's action id of the meter check that settles this row (action check).
    var checkActionID: String?

    init?(json: [String: Any]?) {
        guard let json, let raw = json["action"] as? String, let action = Action(rawValue: raw) else { return nil }
        self.action = action
        summary = (json["summary"] as? String)?.trimmedOrNil
        steps = (json["steps"] as? [String] ?? []).compactMap(\.trimmedOrNil)
        offers = (json["options"] as? [[String: Any]] ?? []).compactMap { FixIt.Option(json: $0) }
        fileFormats = ((json["part_file"] as? [String: Any])?["formats"] as? [String] ?? []).filter { ["stl", "step"].contains($0) }
        checkActionID = (json["check_action_id"] as? String)?.trimmedOrNil
    }
}

extension FixIt.Option {
    /// One offer from a report's `fix` / `fix_plan.options` entry; nil without a title, a seller and an http(s) link.
    init?(json o: [String: Any]) {
        guard let title = (o["title"] as? String)?.trimmedOrNil, let seller = (o["seller"] as? String)?.trimmedOrNil,
              let link = (o["link"] as? String).flatMap(URL.init(string:)), ["http", "https"].contains(link.scheme ?? "")
        else { return nil }
        let price = (o["price"] as? NSNumber)?.doubleValue
        let currency = (o["currency"] as? String)?.trimmedOrNil
        self.init(title: title, price: price, currency: price == nil ? nil : currency, seller: seller, link: link)
    }
}

/// What the person said after trying a fix. The raw values are the contract's (`finding.outcome.state`).
enum OutcomeChoice: String, CaseIterable, Sendable, Codable {
    case fixed
    case notFixed = "not_fixed"
    case notTried = "not_tried"

    /// The button.
    var button: String {
        switch self {
        case .fixed: "Yes"
        case .notFixed: "No"
        case .notTried: "Haven't tried"
        }
    }

    /// The answer in a sentence, for the saved line and for VoiceOver.
    var words: String {
        switch self {
        case .fixed: "It fixed it"
        case .notFixed: "It did not fix it"
        case .notTried: "You have not tried it yet"
        }
    }
}

/// A row's saved answer (report `findings[].outcome`).
struct OutcomeAnswer: Equatable, Sendable {
    var choice: OutcomeChoice
    var recordedAt: String?
    var note: String?

    init(choice: OutcomeChoice, recordedAt: String? = nil, note: String? = nil) {
        self.choice = choice
        self.recordedAt = recordedAt
        self.note = note
    }

    init?(json: [String: Any]?) {
        guard let json, let raw = json["state"] as? String, let choice = OutcomeChoice(rawValue: raw) else { return nil }
        self.init(choice: choice, recordedAt: json["recorded_at"] as? String, note: (json["note"] as? String)?.trimmedOrNil)
    }
}

/// The customer-facing words of the report, built only from report fields: the verdict as one plain sentence, the
/// confidence as a word, a part as "the rear wheel". Pure, so it is unit-tested (CustomerReportTests). No number is
/// ever shown for confidence (A5, I6).
enum CustomerWords {
    // MARK: Parts and problems

    /// "the rear wheel" from the row's part name ("Rear wheel").
    static func thePart(_ p: ReportModel.Problem) -> String {
        let name = p.part.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return "the part" }
        // Keep acronyms, model names and references as written ("USB socket", "J1"): only a plain word is lowered.
        let keep = name.dropFirst().contains { $0.isUppercase || $0.isNumber }
        return "the " + (keep ? name : name.prefix(1).lowercased() + name.dropFirst())
    }

    /// What is wrong with it, as a short predicate ("is missing", "is loose"), from the row's issue code.
    static func predicate(_ code: String) -> String {
        switch code {
        case "missing": "is missing"
        case "damaged": "is damaged"
        case "broken": "is broken"
        case "shifted": "is out of place"
        case "partly": "is only partly seated"
        case "loose": "is loose"
        case "unseated": "is not plugged in all the way"
        case "reversed": "is the wrong way round"
        case "worn": "is worn"
        case "dirty": "is dirty"
        case "open": "has a broken connection"
        case "short": "may be shorted"
        case "out_of_range": "reads out of range"
        case "wrong_pin": "is on the wrong pin"
        case "unexpected_part": "should not be there"
        case "unexpected_wire": "has a wire that should not be there"
        case "colour_mismatch": "is the wrong colour"
        default: "may have a problem"
        }
    }

    /// "The rear wheel is missing". An electrical suspect nobody measured, or any row the design engine is unsure of,
    /// is "may": only a fact the scan or a meter showed is said flat.
    static func statement(_ p: ReportModel.Problem) -> String {
        "\(thePart(p)) \(sentencePredicate(p))".capitalizedFirst
    }

    static let electrical: Set<String> = ["open", "short", "out_of_range", "wrong_pin"]

    static func sentencePredicate(_ p: ReportModel.Problem) -> String {
        let plain = predicate(p.issueCode)
        guard !p.proved else { return plain }
        if electrical.contains(p.issueCode) {
            switch p.issueCode {
            case "open": return "may have a broken connection"
            case "out_of_range": return "may be reading out of range"
            case "wrong_pin": return "may be on the wrong pin"
            default: return plain   // "may be shorted" already hedges
            }
        }
        if p.confidenceWord == "low", plain.hasPrefix("is ") { return "may be " + plain.dropFirst(3) }
        return plain
    }

    // MARK: The verdict

    /// The report's verdict in plain sentences, led by what is wrong: "The rear wheel is missing. Everything else
    /// matches the design." nil when the design engine did not judge the scan (the stage's own headline stands) or
    /// when there is no plain way to say it (setup rows, not enough evidence).
    static func verdictSentence(_ r: ReportModel) -> String? {
        guard r.design != nil else { return nil }
        let unseen = r.notInspected.count      // parts the scan could not see from outside
        let unsettled = r.insufficient.count   // checks the design engine could not decide
        let open = unsettled == 1 ? "1 check could not be completed." : "\(unsettled) checks could not be completed."
        switch r.verdict {
        case .potentialIssues:
            let top = r.mostLikely
            guard let first = top.first else { return nil }
            var lead: String
            switch top.count {
            case 1: lead = statement(first) + "."
            case 2: lead = statement(first) + " and " + lowerFirst(statement(top[1])) + "."
            default:
                let more = top.count - 2
                lead = statement(first) + ", " + lowerFirst(statement(top[1])) + " and "
                    + (more == 1 ? "1 other thing needs" : "\(more) other things need") + " a look."
            }
            if !r.otherIssues.isEmpty {
                let n = r.otherIssues.count
                lead += " " + (n == 1 ? "1 more thing is worth a look." : "\(n) more things are worth a look.")
            } else if unsettled > 0 {
                lead += " " + open   // never "everything else matches" while a check is undecided
            } else if unseen > 0 {
                lead += " Everything else we could see matches the design."
            } else {
                lead += " Everything else matches the design."
            }
            return lead
        case .looksGood, .noProblemsSeen:
            guard r.setup.isEmpty else { return nil }
            if unsettled > 0 { return "Nothing looked wrong, but " + lowerFirst(open) }
            var s = unseen > 0 ? "Everything we could see matches the design." : "Everything matches the design."
            if unseen > 0 { s += unseen == 1 ? " 1 part could not be seen from outside." : " \(unseen) parts could not be seen from outside." }
            return s
        default:
            return nil
        }
    }

    private static func lowerFirst(_ s: String) -> String { s.prefix(1).lowercased() + s.dropFirst() }

    // MARK: Confidence

    /// The design engine's word (high, medium, low) in the customer's: likely, possible, unsure. A meter-proved row
    /// is "Confirmed with a meter". Never a number.
    static func confidence(_ word: String?, proved: Bool = false) -> String? {
        if proved { return "Confirmed with a meter" }
        switch word?.lowercased() {
        case "high": return "Likely"
        case "medium": return "Possible"
        case "low": return "Unsure"
        default: return nil
        }
    }

    /// One sentence for the card: "How sure we are: likely."
    static func confidenceLine(_ word: String?, proved: Bool = false) -> String? {
        guard let c = confidence(word, proved: proved) else { return nil }
        return proved ? c + "." : "How sure we are: \(c.lowercased())."
    }

    // MARK: What it saw

    /// Where the evidence came from, in words: the photos the crop was cut from, a meter reading, or the scan compared with the design.
    static func evidenceSource(_ p: ReportModel.Problem) -> String {
        if p.proved { return "From a meter reading" }
        if !p.photoLabels.isEmpty { return "From " + p.photoLabels.joined(separator: ", ") }
        return "From the scan, compared with the design"
    }

    // MARK: Fix it with Grain

    /// What the design engine is told, in plain words, so the person sees what is sent: the symptom, the design, the
    /// parts the scan flagged, the readings sent, the fixes suggested so far. The gateway adds the same facts to the
    /// message; this only shows them.
    static func fixContext(_ r: ReportModel, symptom: String?, readingsSent: Int) -> [String] {
        var lines: [String] = []
        if let design = r.design { lines.append("The design: \(design)") }
        if let symptom = symptom?.trimmedOrNil { lines.append("What you told us: \u{201C}\(symptom)\u{201D}") }
        let flagged = r.mostLikely + r.otherIssues
        if !flagged.isEmpty {
            lines.append("What the scan found: " + flagged.map { statement($0).lowercased() }.joined(separator: "; "))
        } else if r.verdict != .insufficientEvidence {
            lines.append("What the scan found: nothing that differs from the design")
        }
        if readingsSent > 0 {
            lines.append(readingsSent == 1 ? "1 meter reading and what the design says about it"
                                           : "\(readingsSent) meter readings and what the design says about them")
        }
        let fixes = (r.mostLikely + r.otherIssues + r.setup).compactMap { p in p.fixPlan?.summary ?? p.fix }
        if !fixes.isEmpty { lines.append("Fixes suggested so far: " + fixes.map { ReportWords.display($0) }.joined(separator: " ")) }
        return lines
    }
}
