import Foundation
import simd

/// What the report screen shows, read leniently from the gateway's report JSON (spec/contracts/report.schema.json)
/// so a new enum value on the backend never blanks the screen. Wording stays the backend's; this only picks,
/// orders and trims it. No probability number is ever produced here (I6): confidence stays a word.
struct ReportModel: Sendable {
    enum Verdict: Sendable, Equatable {
        case potentialIssues, looksGood, insufficientEvidence
        /// V8: nothing judged shows an issue and the capture covered the object well, but some regions were not
        /// checked (listed in `verdict_reason.caveat`). Good news, said with its caveat; not "insufficient".
        case noProblemsSeen
        case other(String)

        init(_ raw: String) {
            switch raw {
            case "potential_issues": self = .potentialIssues
            case "looks_good": self = .looksGood
            case "insufficient_evidence": self = .insufficientEvidence
            case "no_problems_seen": self = .noProblemsSeen
            default: self = .other(raw)
            }
        }

        var words: String {
            switch self {
            case .potentialIssues: "Likely problems found"
            case .looksGood: "Nothing looked wrong"
            case .insufficientEvidence: "Not enough to call it yet"
            case .noProblemsSeen: "No problems seen"
            case .other(let raw): raw.replacingOccurrences(of: "_", with: " ").capitalizedFirst
            }
        }
    }

    enum Severity: String, Sendable {
        case safety, functional, cosmetic

        var words: String {
            switch self {
            case .safety: "Safety"
            case .functional: "Stops it working"
            case .cosmetic: "Cosmetic"
            }
        }
    }

    /// One judged row (a problem in `most_likely`, another issue, or a cosmetic one).
    struct Problem: Identifiable, Sendable {
        var id: String
        /// "Housing"
        var part: String
        /// "Looks damaged"
        var issue: String
        /// Plain words that help find the part in the photo.
        var whereWords: String?
        var severity: Severity?
        /// "Medium confidence"
        var confidence: String?
        /// Jev's likelihood, in words ("Unsure"), when judged.
        var likelihood: String?
        /// What the vision model saw, without the model-name prefix.
        var observation: String?
        /// The remaining evidence lines, as written.
        var evidence: [String]
        /// sha256 hex of each crop blob, first one the main crop.
        var crops: [String]
        var photoLabels: [String]
        var regions: Int
        var locationNote: String?
        /// Where it is on the scan (metres, the scan's ARKit frame, Y up), when the locator computed it.
        var point: SIMD3<Float>?
        /// Half the diagonal of the region the locator found (metres): how big the problem area is on the scan.
        var extent: Float? = nil
        /// A meter reading confirmed the fault (`confirmed_by_meter`).
        var proved: Bool
        /// A meter reading ruled the fault out (`ruled_out_by_meter`): cleared, so never a problem in any list.
        var ruledOut: Bool = false
        /// The main crop's photo and its box there (pixels of the full still, sensor orientation).
        var cropStill: String? = nil
        var cropBox: [Double]? = nil
        /// A setup or assembly state (report `category` setup: a battery missing, a cable unplugged), not a broken part.
        var isSetup: Bool = false
        /// The plain fix for a setup state ("Put the batteries in, then test again.").
        var fix: String? = nil
    }

    /// A region nobody looked at (not_inspected) or that could not be judged (insufficient_evidence).
    struct Gap: Identifiable, Sendable {
        var id: String
        var part: String
        var reason: String
        var detail: String?
        var photoLabel: String?
        var crop: String?
        var regions: Int
    }

    struct SafetyNote: Identifiable, Sendable {
        var id: String { code + text }
        var code: String
        var text: String

        var symbol: String {
            switch code {
            case "heat_damage": "flame.fill"
            case "mains_power": "bolt.fill"
            case "unplug_first": "powerplug.fill"
            case "battery_hazard": "battery.100percent.bolt"
            case "capacitor_charge": "bolt.trianglebadge.exclamationmark.fill"
            default: Symbol.safety
            }
        }
    }

    struct NextStep: Sendable {
        var kind: String
        var instruction: String
        var reason: String?

        var symbol: String {
            switch kind {
            case "request_closeup": Symbol.closeup
            case "request_other_angle": "arrow.triangle.2.circlepath.camera.fill"
            case "rescan": Symbol.scan
            case "run_continuity_test", "run_resistance_test", "run_voltage_test": "gauge.with.dots.needle.50percent"
            case "ask_user": "questionmark.bubble.fill"
            default: Symbol.nextStep
            }
        }

        var title: String {
            switch kind {
            case "request_closeup": "Take a closer photo"
            case "request_other_angle": "Take another angle"
            case "rescan": "Scan it again"
            case "run_continuity_test", "run_resistance_test", "run_voltage_test": "Check it with a meter"
            case "ask_user": "Tell us more"
            default: "Next step"
            }
        }
    }

    var verdict: Verdict
    var verdictReason: String?
    /// `verdict_reason.caveat` (no_problems_seen): what could not be checked, in the backend's words.
    var verdictCaveat: String?
    var summary: String?
    var safety: [SafetyNote]
    var mostLikely: [Problem]
    /// Issue rows after the top three (still suspects, lower ranked).
    var otherIssues: [Problem]
    var cosmetic: [Problem]
    /// Setup and assembly states (report `setup`), each with its fix: listed apart, never counted as broken.
    var setup: [Problem]
    var notInspected: [Gap]
    var insufficient: [Gap]
    var nextStep: NextStep?
    var limits: [String]
    var photos: Int
    var photosInspected: Int
    var coverageWords: String?
    var createdAt: Date?
    /// What the object is (report `object`): "Category · Brand Model", or the category alone; nil when unknown.
    var objectWords: String?
    /// The backend's choice for the hero (report `mesh.hero`): points, mesh or photos; and the mesh revision.
    var hero: String?
    var meshRev: Int?
    /// "Fix it" (report `fix`): where to buy the top finding's part, when the backend's search found offers.
    var fix: FixIt?
    /// The person's own guess and what the photos showed about it (report `user_lead`).
    var userLead: GuessCheck?
    /// Photos from the bottom scan that could not be lined up with the model (report `bottom_not_placed`): real
    /// findings without a 3D point, shown as photos so the underside is never a dead end. One per crop.
    var bottomPhotos: [BottomPhoto] = []

    struct BottomPhoto: Identifiable, Sendable, Equatable {
        var id: String
        var part: String
        var crop: String
    }

    init(json r: [String: Any]) {
        verdict = Verdict(r["verdict"] as? String ?? "")
        verdictReason = (r["verdict_reason"] as? [String: Any])?["text"] as? String
        verdictCaveat = ((r["verdict_reason"] as? [String: Any])?["caveat"] as? String)?.trimmedOrNil
        summary = (r["summary"] as? [String: Any])?["text"] as? String
        safety = (r["safety"] as? [[String: Any]] ?? []).compactMap { s in
            (s["text"] as? String).map { SafetyNote(code: s["code"] as? String ?? "", text: $0) }
        }
        let findings = Self.current(r["findings"]).map(Self.problem)
        // As the backend counts issues: a fault a meter reading ruled out is cleared, not a problem (when every
        // finding is ruled out `most_likely` is empty, and the fallback must not bring them back as problems).
        let issues = findings.filter { $0.issue != Self.noIssue && !$0.ruledOut }
        let topIDs: [String] = (r["most_likely"] as? [Any] ?? []).compactMap {
            ($0 as? String) ?? (($0 as? [String: Any])?["finding_id"] as? String)
        }
        if topIDs.isEmpty {
            mostLikely = Array(issues.prefix(3))
        } else {
            mostLikely = topIDs.prefix(3).compactMap { id in findings.first { $0.id == id && !$0.ruledOut } }
        }
        let shown = Set(mostLikely.map(\.id))
        otherIssues = issues.filter { !shown.contains($0.id) }
        cosmetic = Self.current(r["cosmetic"]).map(Self.problem)
        setup = Self.current(r["setup"]).map(Self.problem)
        notInspected = Self.current(r["not_inspected"]).map(Self.gap)
        insufficient = Self.current(r["insufficient_evidence"]).map(Self.gap)
        if let n = r["next_step"] as? [String: Any], let instruction = n["instruction"] as? String {
            nextStep = NextStep(kind: n["kind"] as? String ?? "", instruction: instruction, reason: n["reason"] as? String)
        }
        limits = r["limits"] as? [String] ?? []
        let capture = r["capture"] as? [String: Any] ?? [:]
        photos = capture["stills"] as? Int ?? 0
        photosInspected = capture["stills_inspected"] as? Int ?? 0
        coverageWords = (capture["coverage_words"] as? String).flatMap { $0 == "not measured" ? nil : $0 }
        createdAt = (r["created_at"] as? String).flatMap { try? Date($0, strategy: .iso8601) }
        objectWords = Self.objectWords(r["object"] as? [String: Any])
        hero = (r["mesh"] as? [String: Any])?["hero"] as? String
        meshRev = (r["mesh"] as? [String: Any])?["rev"] as? Int
        fix = FixIt(json: r["fix"] as? [String: Any])
        userLead = GuessCheck(json: r["user_lead"] as? [String: Any])
        var seen = Set<String>()
        bottomPhotos = ((r["bottom_not_placed"] as? [[String: Any]]) ?? []).compactMap { b in
            guard let sha = Self.sha((b["photo"] as? [String: Any])?["crop_ref"]), seen.insert(sha).inserted else { return nil }
            let part = (b["part_class"] as? String).map { $0.replacingOccurrences(of: "_", with: " ").capitalizedFirst } ?? "Bottom"
            return BottomPhoto(id: b["finding_id"] as? String ?? sha, part: part, crop: sha)
        }
    }

    /// Every crop the report shows (problems, cosmetic, gaps), for saving them on the phone.
    var cropSHAs: [String] {
        var out: [String] = []
        for sha in (mostLikely + otherIssues + setup + cosmetic).flatMap(\.crops) + (notInspected + insufficient).compactMap(\.crop)
                   + bottomPhotos.map(\.crop)
        where !out.contains(sha) { out.append(sha) }
        return out
    }

    /// The backend's own generic words: the category, then brand and model when it gave them.
    static func objectWords(_ o: [String: Any]?) -> String? {
        guard let o, let category = (o["category"] as? String)?.trimmedOrNil, category != "unknown" else { return nil }
        let named = [o["brand"] as? String, o["model"] as? String].compactMap { $0?.trimmedOrNil }.joined(separator: " ")
        let kind = category.replacingOccurrences(of: "_", with: " ").capitalizedFirst
        return named.isEmpty ? kind : "\(kind) · \(named)"
    }

    /// Rows to show from a report section after a rebuild: a row that another row names in `superseded_ids` (a
    /// newer judgement of the same thing) or `finding_ids` (a grouped "could not judge" row) is dropped, and each
    /// finding id appears once.
    static func current(_ section: Any?) -> [[String: Any]] {
        let rows = section as? [[String: Any]] ?? []
        var replaced = Set<String>()
        for row in rows {
            let own = row["finding_id"] as? String
            for key in ["superseded_ids", "finding_ids"] {
                for id in row[key] as? [String] ?? [] where id != own { replaced.insert(id) }
            }
        }
        var seen = Set<String>()
        return rows.filter { row in
            guard let id = row["finding_id"] as? String else { return true }
            return !replaced.contains(id) && seen.insert(id).inserted
        }
    }

    /// The issue words of a judged region with no visible issue.
    static let noIssue = "No visible issue"

    // MARK: - Parsing

    private static func problem(_ f: [String: Any]) -> Problem {
        let raw = (f["evidence"] as? [String]) ?? []
        let observation = raw.lazy.compactMap(Self.modelObservation).first
        let evidence = raw.filter { Self.modelObservation($0) == nil }
        var crops: [String] = []
        for c in [(f["crop"] as? [String: Any])] + ((f["crops"] as? [[String: Any]]) ?? []).map(Optional.some) {
            if let sha = Self.sha(c?["crop_ref"]), !crops.contains(sha) { crops.append(sha) }
        }
        var labels: [String] = []
        for c in [(f["crop"] as? [String: Any])] + ((f["crops"] as? [[String: Any]]) ?? []).map(Optional.some) {
            if let id = c?["still_id"] as? String, !labels.contains(photoLabel(id)) { labels.append(photoLabel(id)) }
        }
        let judgment = f["judgment"] as? [String: Any]
        let location = f["location"] as? [String: Any]
        let proof = (f["proof"] as? [String: Any])?["status"] as? String
        return Problem(
            id: f["finding_id"] as? String ?? UUID().uuidString,
            part: (f["part_class"] as? String ?? "Part").replacingOccurrences(of: "_", with: " ").capitalizedFirst,
            issue: issueWords(f["issue"] as? String),
            whereWords: (f["part_words"] as? String).map(\.capitalizedFirst),
            severity: (f["severity"] as? String).flatMap(Severity.init(rawValue:)),
            confidence: (f["confidence_word"] as? String).map { "\($0.capitalizedFirst) confidence" },
            likelihood: (judgment?["likelihood_word"] as? String).map { $0.replacingOccurrences(of: "_", with: " ").capitalizedFirst },
            observation: observation,
            evidence: evidence,
            crops: crops,
            photoLabels: labels,
            regions: f["regions"] as? Int ?? 1,
            locationNote: location?["status"] as? String == "computed" ? nil : location?["reason"] as? String,
            point: Self.point(location),
            extent: Self.extent(location),
            proved: proof == "confirmed_by_meter",
            ruledOut: proof == "ruled_out_by_meter",
            cropStill: (f["crop"] as? [String: Any])?["still_id"] as? String,
            cropBox: ((f["crop"] as? [String: Any])?["box_px"] as? [NSNumber])?.map(\.doubleValue),
            isSetup: f["category"] as? String == "setup",
            fix: (f["fix"] as? String)?.trimmedOrNil)
    }

    private static func gap(_ g: [String: Any]) -> Gap {
        let photo = g["photo"] as? [String: Any]
        let parts = (g["reason"] as? String ?? "").components(separatedBy: "; ")
        // "could not judge the X in still s11; vision model (…): The crop is blurry…; detector region …"
        let observation = parts.lazy.compactMap(Self.modelObservation).first
        let lead = parts.first.map { Self.modelObservation($0) ?? $0 } ?? ""
        let rest = parts.dropFirst().filter { Self.modelObservation($0) == nil && !$0.hasPrefix("detector ") }
        let reason = ([lead] + rest).filter { !$0.isEmpty }.joined(separator: "; ")
        return Gap(
            id: g["finding_id"] as? String ?? UUID().uuidString,
            part: (g["part_class"] as? String ?? "Region").replacingOccurrences(of: "_", with: " ").capitalizedFirst,
            reason: (reason.isEmpty ? "No reason given" : reason).capitalizedFirst,
            detail: observation == lead ? nil : observation,
            photoLabel: (photo?["still_id"] as? String).map(photoLabel),
            crop: sha(photo?["crop_ref"]),
            regions: g["regions"] as? Int ?? 1)
    }

    static func issueWords(_ issue: String?) -> String {
        switch issue ?? "" {
        case "none": noIssue
        case "loose": "May be loose"
        case "unseated": "Not plugged in fully"
        case "reversed": "In the wrong way round"
        case "open": "Looks open"
        case "short": "May be shorted"
        case "partly": "Partly seated"
        case "out_of_range": "Out of range"
        case "unexpected_part": "Unexpected part"
        case "unexpected_wire": "Unexpected wire"
        case "wrong_pin": "On the wrong pin"
        case "colour_mismatch": "Colour doesn't match"
        case "": "Possible issue"
        case let other: "Looks \(other.replacingOccurrences(of: "_", with: " "))"
        }
    }

    /// "vision model (<id>): The left edge…" → "The left edge…". nil for any other line.
    static func modelObservation(_ line: String) -> String? {
        guard line.hasPrefix("vision model"), let r = line.range(of: "): ") ?? line.range(of: ": ") else { return nil }
        let text = line[r.upperBound...].trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : text
    }

    /// "s8" or "scan_ab12_s8" → "Photo 8"; any other id is shown as is.
    static func photoLabel(_ stillID: String) -> String {
        let tail = stillID.split(separator: "_").last.map(String.init) ?? stillID
        if tail.hasPrefix("s"), let n = Int(tail.dropFirst()) { return "Photo \(n)" }
        return "Photo \(stillID)"
    }

    /// `location.point_m` when `status` is computed.
    static func point(_ location: [String: Any]?) -> SIMD3<Float>? {
        guard location?["status"] as? String == "computed", let p = location?["point_m"] as? [Any], p.count == 3 else { return nil }
        let v = p.compactMap { ($0 as? NSNumber)?.floatValue }
        return v.count == 3 ? SIMD3(v[0], v[1], v[2]) : nil
    }

    /// Half the diagonal of `location.region_min_m`...`region_max_m` when the point was computed.
    static func extent(_ location: [String: Any]?) -> Float? {
        guard point(location) != nil,
              let lo = (location?["region_min_m"] as? [Any])?.compactMap({ ($0 as? NSNumber)?.floatValue }), lo.count == 3,
              let hi = (location?["region_max_m"] as? [Any])?.compactMap({ ($0 as? NSNumber)?.floatValue }), hi.count == 3
        else { return nil }
        return simd_distance(SIMD3(lo[0], lo[1], lo[2]), SIMD3(hi[0], hi[1], hi[2])) / 2
    }

    static func sha(_ ref: Any?) -> String? {
        guard let ref = ref as? String, ref.hasPrefix("blob://sha256:") else { return nil }
        return String(ref.dropFirst("blob://sha256:".count))
    }
}

extension String {
    /// First letter upper-cased, the rest untouched ("USB socket" stays "USB socket").
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
