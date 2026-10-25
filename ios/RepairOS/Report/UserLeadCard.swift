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
        case .confirmed: ("Photos agree", "checkmark.circle.fill", .caution)
        case .notSeen: ("Not seen in the photos", "eye", .good)
        case .couldNotCheck: ("Could not check", "questionmark.circle", .accent)
        }
    }
}

struct UserLeadCard: View {
    let lead: GuessCheck

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("What you suspected").font(.pillLabel).foregroundStyle(.secondary)
            Text("“\(lead.said)”").font(.subheadline).italic().fixedSize(horizontal: false, vertical: true)
            Divider()
            HStack(spacing: Spacing.xs) {
                Image(systemName: lead.badge.symbol).foregroundStyle(lead.badge.tone.color).accessibilityHidden(true)
                Text("What Lea found · \(lead.badge.words)").font(.pillLabel).foregroundStyle(lead.badge.tone.text)
            }
            Text(lead.note).font(.subheadline).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
    }
}
