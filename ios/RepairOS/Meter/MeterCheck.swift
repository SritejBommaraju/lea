import Foundation

/// What a good reading looks like for one meter check (report `next_step.expected`): the design's own range, shown to
/// the customer in words before they measure and beside the verdict after. The app never judges the reading; the
/// design engine does. This only says what it will be judged against.
struct ExpectedReading: Equatable, Sendable {
    var voltsMin: Double?
    var voltsMax: Double?
    var ohmsMax: Double?
    var continuity: Bool?

    init(voltsMin: Double? = nil, voltsMax: Double? = nil, ohmsMax: Double? = nil, continuity: Bool? = nil) {
        self.voltsMin = voltsMin
        self.voltsMax = voltsMax
        self.ohmsMax = ohmsMax
        self.continuity = continuity
    }

    /// nil when the step names no usable range.
    init?(json: [String: Any]?) {
        guard let json else { return nil }
        let e = ExpectedReading(voltsMin: (json["volts_min"] as? NSNumber)?.doubleValue,
                                voltsMax: (json["volts_max"] as? NSNumber)?.doubleValue,
                                ohmsMax: (json["ohms_max"] as? NSNumber)?.doubleValue,
                                continuity: json["continuity"] as? Bool)
        guard e.voltsMin != nil || e.voltsMax != nil || e.ohmsMax != nil || e.continuity != nil else { return nil }
        self = e
    }

    /// "between 4.75 and 5.25 V", "about 5 V or more", "under 1 \u{3A9}", "it should beep".
    var words: String? {
        switch (voltsMin, voltsMax) {
        case let (lo?, hi?): return "between \(Self.number(lo)) and \(Self.number(hi)) V"
        case let (lo?, nil): return "\(Self.number(lo)) V or more"
        case let (nil, hi?): return "\(Self.number(hi)) V or less"
        default: break
        }
        if let ohmsMax { return "\(Self.number(ohmsMax)) \u{3A9} or less" }
        if let continuity { return continuity ? "the meter should beep (a connection)" : "the meter should not beep (no connection)" }
        return nil
    }

    /// 4.75 -> "4.75", 5.0 -> "5", 0.30000000000000004 -> "0.3", 0.0001 -> "0.0001": six significant digits, so a
    /// small target is never shown as zero.
    static func number(_ x: Double) -> String {
        String(format: "%.6g", x)
    }
}

/// The words of a meter check for someone who has never read a schematic.
enum MeterWords {
    /// "J1.1" -> "J1, pin 1"; a name without a pin number is returned as it is.
    static func pin(_ id: String) -> String {
        let parts = id.split(separator: ".", maxSplits: 1).map(String.init)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return id }
        return "\(parts[0]), pin \(parts[1])"
    }

    /// The two places to touch, in order: the first is the red probe, the second the black. Voltage is read red on the
    /// positive point; continuity and resistance work either way round, so only "touch one probe on each" is said.
    static func probes(pins: [String], kind: MeterReadingKind) -> [String] {
        guard pins.count == 2 else { return [] }
        if kind == .voltageDc {
            return ["Red probe on \(pin(pins[0]))", "Black probe on \(pin(pins[1]))"]
        }
        return ["Touch one probe on \(pin(pins[0]))", "Touch the other probe on \(pin(pins[1]))"]
    }

    /// What to set the meter dial to.
    static func dial(_ kind: MeterReadingKind) -> String {
        switch kind {
        case .continuity: "Set the meter to continuity (the beep symbol)."
        case .voltageDc: "Set the meter to DC volts (the V with a straight line)."
        case .resistance: "Set the meter to ohms (\u{3A9})."
        }
    }

    /// The unit the typed value is in.
    static func unit(_ kind: MeterReadingKind) -> String? {
        switch kind {
        case .voltageDc: "V"
        case .resistance: "\u{3A9}"
        case .continuity: nil
        }
    }
}

/// The design engine's verdict on one reading (GET /v1/readings/{id} -> `verdict`).
struct ReadingVerdict: Equatable, Sendable {
    /// `pending` until the judgement is back, then `judged`; `failed` or `unavailable` when it will not come.
    var state: String
    /// ok | off | open | wrong_pin | unknown_net | no_expected | invalid, when judged.
    var verdict: String?
    var notes: [String]

    init(state: String, verdict: String? = nil, notes: [String] = []) {
        self.state = state
        self.verdict = verdict
        self.notes = notes
    }

    init?(json: [String: Any]?) {
        guard let json, let state = json["state"] as? String else { return nil }
        self.init(state: state, verdict: json["verdict"] as? String, notes: (json["notes"] as? [String] ?? []).compactMap(\.trimmedOrNil))
    }

    var isFinal: Bool { state != "pending" }

    /// Pass, fail, or neither: only a judged `ok` passes, only a judged mismatch fails.
    enum Outcome: Equatable, Sendable { case pass, fail, unjudged }

    var outcome: Outcome {
        guard state == "judged" else { return .unjudged }
        switch verdict {
        case "ok": return .pass
        case "off", "open", "wrong_pin": return .fail
        default: return .unjudged
        }
    }

    /// One line, with the design's range beside a fail.
    func headline(expected: ExpectedReading?) -> String {
        switch state {
        case "pending": return "Saved. Grain is checking it against the design\u{2026}"
        case "failed": return "Saved, but Grain could not check it. Try again from the report."
        case "unavailable": return "Saved on this phone. This server has no design to check it against."
        default: break
        }
        let range = expected?.words.map { " It should be \($0)." } ?? ""
        switch verdict {
        case "ok": return "Matches the design." + (expected?.words.map { " It should be \($0)." } ?? "")
        case "off": return "Does not match the design." + range
        case "open": return "No connection where the design has one." + range
        case "wrong_pin": return "That reading is on a different pin than the design expects."
        case "unknown_net": return "The design has no net with that name, so Grain could not check it."
        case "no_expected": return "The design does not say what this should read, so Grain could not check it."
        default: return "Grain could not check this reading."
        }
    }
}
