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

/// "Fix it" as a section on the canvas: the part in `cardTitle`, named as in the problem card, then one grouped list of
/// offers (title, seller, price as the backend gives it) and the replacement guide.
struct FixItCard: View {
    let fix: FixIt?
    let searching: Bool
    /// The backend skipped the search (its spend cap): a quiet line instead of offers.
    var unavailable = false
    /// Opens "Fix it with Grain" (nil when the report was not judged against a design, or there is no server).
    var askDesign: (() -> Void)? = nil
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            offers
            if let askDesign { askRow(askDesign) }
        }
    }

    private func askRow(_ open: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if fix == nil { SectionHeader("Fix it") }
            GroupedList {
                Button(action: open) {
                    HStack {
                        Label("Fix it with Grain", systemImage: "wand.and.stars")
                            .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                    }
                    .padding(.vertical, Spacing.m)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableCardStyle())
                .accessibilityHint("Asks the design engine to change the product's design")
            }
        }
    }

    @ViewBuilder private var offers: some View {
        if let fix {
            VStack(alignment: .leading, spacing: Spacing.s) {
                SectionHeader("Fix it")
                Text(fix.part.capitalizedFirst).display(.card).foregroundStyle(Palette.ink)
                GroupedList {
                    ForEach(fix.options) { o in
                        Button { openURL(o.link) } label: { row(o) }
                            .buttonStyle(PressableCardStyle())
                            .accessibilityHint("Opens the offer in Safari")
                    }
                    if let guide = fix.guide {
                        Button { openURL(guide) } label: {
                            HStack {
                                Label("Replacement guide", systemImage: "book.pages")
                                    .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                                Spacer(minLength: 0)
                                Image(systemName: "arrow.up.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                            }
                            .padding(.vertical, Spacing.m)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableCardStyle())
                    }
                }
                Text("Prices from a web search; check fit and seller before you buy.")
                    .font(.caption).foregroundStyle(Palette.secondary)
            }
            .transition(.opacity)
        } else if searching {
            HStack(spacing: Spacing.m) {
                ProgressView().controlSize(.small).tint(Palette.accent)
                Text("Searching for parts…").font(.subheadline).foregroundStyle(Palette.secondary)
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44)
            .transition(.opacity)
            .accessibilityElement(children: .combine)
        } else if unavailable {
            Label("Parts search is not available right now.", systemImage: "cart")
                .font(.subheadline).foregroundStyle(Palette.secondary)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .transition(.opacity)
        }
    }

    private func row(_ o: FixIt.Option) -> some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text(o.title).font(.subheadline).foregroundStyle(Palette.ink).lineLimit(2).multilineTextAlignment(.leading)
                Text(o.seller).font(.footnote).foregroundStyle(Palette.secondary).lineLimit(1)
            }
            Spacer(minLength: Spacing.s)
            if let price = o.priceWords {
                Text(price).font(.subheadline.weight(.semibold)).monospacedDigit().foregroundStyle(Palette.ink)
            }
            Image(systemName: "arrow.up.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
        }
        .padding(.vertical, Spacing.m)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}
