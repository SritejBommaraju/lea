import SwiftUI

/// First-run consent: where the photos go before anyone scans. Shown once (and again from Settings, read-only).
/// Bump `Consent.version` when what it says changes, so everyone sees the new wording.
enum Consent {
    static let version = 2
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
                    Text("Your photos, and who sees them").display(.screen).foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                    Text("lea needs a few photos of the broken thing to work out what's wrong. Here is where they go.")
                        .font(.body).foregroundStyle(Palette.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                GroupedList {
                    ConsentRow(symbol: Symbol.mac, title: "First, to the lea server",
                               text: "Photos and the scan go to the lea server this phone is connected to.")
                    ConsentRow(symbol: "sparkle.magnifyingglass", title: "Then analysed by Claude and Jev",
                               text: "The server sends the photos to Anthropic (Claude) and TypeSafe (Jev), which look for problems and help write the report.")
                    ConsentRow(symbol: "location.slash", title: "Location and photo details removed",
                               text: "EXIF data, including GPS location, is removed from the photos before they are analysed.")
                    ConsentRow(symbol: "eye", title: "Lea sees the camera when you talk to her",
                               text: "While you scan and talk to Lea, a few camera frames go with your question so she can see what you mean. Only while you are talking with her (\"Lea is looking\" shows), never before \"Hey Lea\".")
                    ConsentRow(symbol: Symbol.privacy, title: "Only what you scan",
                               text: "Nothing is captured until you start a scan. Keep faces, papers and screens out of the frame.")
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .padding(.top, Spacing.xxl)
            .padding(.bottom, Spacing.xl)
            .frame(maxWidth: 600, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollBounceBehavior(.basedOnSize)
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
