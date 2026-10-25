import SwiftUI

/// The report's `user_lead`: what the person suspected and what the photos showed about it. The guess only steered
/// where to look; the words here are the backend's, written from the findings it cites.
struct GuessCheck: Equatable, Sendable {
    enum Checked: String, Sendable { case confirmed, notSeen = "not_seen", couldNotCheck = "could_not_check" }
    var said: String
    var suspect: String?
    var checked: Checked
    var note: String
    var evidenceIDs: [String]

    init?(json: [String: Any]?) {
        guard let json, let said = (json["said"] as? String)?.trimmedOrNil, let note = (json["note"] as? String)?.trimmedOrNil,
              let checked = (json["checked"] as? String).flatMap(Checked.init(rawValue:)) else { return nil }
        self.said = said
        self.note = note
        self.checked = checked
        suspect = (json["suspect"] as? String)?.trimmedOrNil
        evidenceIDs = json["evidence_finding_ids"] as? [String] ?? []
    }

    var badge: (words: String, symbol: String, tone: Tone) {
        switch checked {
        case .confirmed: ("Photos agree", "checkmark.circle", .caution)
        case .notSeen: ("Not seen in the photos", "eye", .neutral)
        case .couldNotCheck: ("Could not check", Symbol.unknown, .neutral)
        }
    }

    /// Shown on the report only when the photos said something about it (no coverage caveat by default: "could not
    /// check" stays off the default path, and Lea answers when asked).
    var shownByDefault: Bool { checked != .couldNotCheck }
}

/// "What you suspected": your words in quotes, then a ruled row with what Lea found and the backend's note. A section
/// on the canvas, not a box; upright type (no italic asides).
struct UserLeadCard: View {
    let lead: GuessCheck

    var body: some View {
        if lead.shownByDefault {
            VStack(alignment: .leading, spacing: Spacing.s) {
                SectionHeader("What you suspected")
                Text("\u{201C}\(lead.said)\u{201D}").font(.body).foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Rectangle().fill(Palette.hairline).frame(height: Line.hairline)
                ToneTag(text: "What Lea found · \(lead.badge.words)", tone: lead.badge.tone, symbol: lead.badge.symbol)
                Text(ReportWords.display(lead.note)).font(.subheadline).foregroundStyle(Palette.ink).fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
        }
    }
}
