import SwiftUI

/// First-run consent: where the photos go before anyone scans. Shown once (and again from Settings, read-only).
/// Bump `Consent.version` when what it says changes, so everyone sees the new wording.
enum Consent {
    static let version = 3
    static let key = "consent.accepted.version"

    static var accepted: Bool { UserDefaults.standard.integer(forKey: key) >= version }
    static func accept() { UserDefaults.standard.set(version, forKey: key) }
}

struct ConsentView: View {
    /// nil = read-only (from Settings): one Done button instead of agree / not now.
    var onAnswer: ((Bool) -> Void)?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Text("Where your photos go").display(.screen).foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                    Text("lea needs photos of your product to work out what's wrong. Here is who gets them.")
                        .font(.body).foregroundStyle(Palette.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                GroupedList {
                    ConsentRow(symbol: Symbol.mac, title: "First, to the lea server",
                               text: "Your photos and the scan go to the lea server this phone is connected to.")
                    ConsentRow(symbol: "sparkle.magnifyingglass", title: "Then checked by AI services",
                               text: "The server shares the photos with the AI services it uses to look for problems and write the report. Ask whoever runs your server which ones.")
                    ConsentRow(symbol: "location.slash", title: "Your location is not sent",
                               text: "The photo details that can include where you were, such as GPS, are removed before anyone checks the photos.")
                    ConsentRow(symbol: "eye", title: "Lea sees what you point at when you talk to her",
                               text: "While you scan and talk to Lea, a few camera frames go with your question so she can see what you mean. This only happens while you are talking with her. \"Lea is looking\" shows on screen, and nothing is sent before you say \"Hey Lea\".")
                    ConsentRow(symbol: Symbol.privacy, title: "Only what you scan",
                               text: "Nothing is captured until you start a scan. Keep faces, papers and screens out of the picture.")
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .padding(.top, Spacing.xxl)
            .padding(.bottom, Spacing.xl)
            .frame(maxWidth: 600, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .bounceBasedOnSize()
        .safeAreaInset(edge: .bottom) {
            GlassBar {
                if let onAnswer {
                    Button("Agree and continue") { Consent.accept(); onAnswer(true) }
                        .buttonStyle(.primary)
                    Button("Not now") { onAnswer(false) }
                        .buttonStyle(.textAction)
                } else {
                    Button("Done") { dismiss() }.buttonStyle(.primary)
                }
            }
        }
        .background(Palette.canvas.ignoresSafeArea())
        .interactiveDismissDisabled(onAnswer != nil)
    }
}

/// One consent row: a bare outline icon, the heading and what it means, on the grouped surface.
private struct ConsentRow: View {
    let symbol: String
    let title: String
    let text: String
    @ScaledMetric(relativeTo: .body) private var iconWidth: CGFloat = 24

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Image(systemName: symbol).font(.body).foregroundStyle(Palette.secondary)
                .frame(width: iconWidth).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.body.weight(.semibold)).foregroundStyle(Palette.ink)
                Text(text).font(.subheadline).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, Spacing.m)
        .accessibilityElement(children: .combine)
    }
}

#Preview { ConsentView(onAnswer: { _ in }) }
