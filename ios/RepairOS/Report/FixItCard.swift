import SwiftUI

/// "Fix it": where to buy the part of the report's top finding (report `fix`, or the live `fix_ready` event when the
/// search finished after the report). Every word about the part and the offers comes from the backend; the app only
/// lays them out. The backend never delays the report for it, so the card says it is searching until the event
/// arrives; it is left out when nothing was found, and says "not available right now" at the backend's spend cap.
struct FixIt: Equatable, Sendable {
    struct Option: Equatable, Sendable, Identifiable {
        var id: String { link.absoluteString }
        var title: String
        var price: Double?
        var currency: String?
        var seller: String
        var link: URL

        /// "$12.99" in the offer's own currency; nil when the result showed no price.
        var priceWords: String? {
            guard let price, let currency else { return nil }
            return price.formatted(.currency(code: currency).precision(.fractionLength(price.rounded() == price ? 0 : 2)))
        }
    }

    var part: String
    var options: [Option]
    var guide: URL?
    var searchedAt: String?

    /// From the report's `fix` or the `fix_ready` payload (same fields). nil without at least one usable offer.
    init?(json: [String: Any]?) {
        guard let json, let part = (json["part"] as? String)?.trimmedOrNil else { return nil }
        let options: [Option] = (json["options"] as? [[String: Any]] ?? []).prefix(3).compactMap { o in
            guard let title = (o["title"] as? String)?.trimmedOrNil, let seller = (o["seller"] as? String)?.trimmedOrNil,
                  let link = (o["link"] as? String).flatMap(URL.init(string:)), ["http", "https"].contains(link.scheme ?? "")
            else { return nil }
            let price = (o["price"] as? NSNumber)?.doubleValue
            let currency = (o["currency"] as? String)?.trimmedOrNil
            return Option(title: title, price: price, currency: price == nil ? nil : currency, seller: seller, link: link)
        }
        guard !options.isEmpty else { return nil }
        self.part = part
        self.options = options
        guide = (json["guide_link"] as? String).flatMap(URL.init(string:)).flatMap { ["http", "https"].contains($0.scheme ?? "") ? $0 : nil }
        searchedAt = json["searched_at"] as? String
    }

    /// The report's `fix` fields, to keep a fix that arrived live with the cached report.
    var json: [String: Any] {
        var out: [String: Any] = ["part": part, "options": options.map { o -> [String: Any] in
            var d: [String: Any] = ["title": o.title, "seller": o.seller, "link": o.link.absoluteString]
            if let p = o.price, let c = o.currency { d["price"] = p; d["currency"] = c }
            return d
        }]
        if let guide { out["guide_link"] = guide.absoluteString }
        if let searchedAt { out["searched_at"] = searchedAt }
        return out
    }
}

struct FixItCard: View {
    let fix: FixIt?
    let searching: Bool
    /// The backend skipped the search (its spend cap): a quiet line instead of offers.
    var unavailable = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        if let fix {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: Spacing.m) {
                    IconBadge(symbol: "wrench.and.screwdriver.fill", tone: .accent)
                    VStack(alignment: .leading, spacing: Spacing.xxs) {
                        Text("Fix it").font(.pillLabel).foregroundStyle(Palette.accent)
                        Text(fix.part.capitalizedFirst).font(.headline)
                    }
                }
                .padding(.bottom, Spacing.s)
                ForEach(fix.options) { o in
                    Divider()
                    Button { openURL(o.link) } label: { row(o) }
                        .buttonStyle(PressableCardStyle())
                        .accessibilityHint("Opens the offer in Safari")
                }
                if let guide = fix.guide {
                    Divider()
                    Button { openURL(guide) } label: {
                        HStack {
                            Label("Replacement guide", systemImage: "book.pages")
                                .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                            Spacer(minLength: 0)
                            Image(systemName: "arrow.up.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }
                        .padding(.vertical, Spacing.m)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableCardStyle())
                }
                Text("Prices from a web search; check fit and seller before you buy.")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.top, Spacing.xs)
            }
            .card(tone: .accent)
            .transition(.opacity)
        } else if searching {
            HStack(spacing: Spacing.m) {
                ProgressView().controlSize(.small).tint(Palette.accent)
                Text("Searching for parts…").font(.subheadline).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            .card()
            .transition(.opacity)
            .accessibilityElement(children: .combine)
        } else if unavailable {
            Label("Parts search is not available right now.", systemImage: "cart")
                .font(.subheadline).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .card()
                .transition(.opacity)
        }
    }

    private func row(_ o: FixIt.Option) -> some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text(o.title).font(.subheadline).foregroundStyle(.primary).lineLimit(2).multilineTextAlignment(.leading)
                Text(o.seller).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: Spacing.s)
            if let price = o.priceWords {
                Text(price).font(.system(.subheadline, design: .rounded, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(.primary)
            }
            Image(systemName: "arrow.up.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .padding(.vertical, Spacing.m)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}
