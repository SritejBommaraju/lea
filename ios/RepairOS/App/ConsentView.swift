import SwiftUI

/// First-run consent: where the photos go before anyone scans. Shown once (and again from Settings, read-only).
/// Bump `Consent.version` when what it says changes, so everyone sees the new wording.
enum Consent {
    static let version = 1
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
            VStack(alignment: .leading, spacing: Spacing.xxl) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ZStack {
                        Circle().fill(Palette.accent.opacity(0.12))
                        Image(systemName: "photo.badge.checkmark.fill")
                            .font(.system(size: 34, weight: .semibold))
                            .foregroundStyle(Palette.accent)
                            .symbolRenderingMode(.hierarchical)
                    }
                    .frame(width: 76, height: 76)
                    .accessibilityHidden(true)
                    Text("Your photos, and who sees them").font(.heroTitle)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("lea needs a few photos of the broken thing to work out what's wrong. Here is where they go.")
                        .font(.body).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: Spacing.xl) {
                    ConsentRow(symbol: Symbol.mac, tone: .accent, title: "First, to the lea server",
                               text: "Photos and the scan go to the lea server this phone is connected to.")
                    ConsentRow(symbol: "sparkle.magnifyingglass", tone: .accent, title: "Then analysed by Claude and Jev",
                               text: "The server sends the photos to Anthropic (Claude) and TypeSafe (Jev), which look for problems and help write the report.")
                    ConsentRow(symbol: "location.slash.fill", tone: .good, title: "Location and photo details removed",
                               text: "EXIF data, including GPS location, is removed from the photos before they are analysed.")
                    ConsentRow(symbol: Symbol.privacy, tone: .good, title: "Only what you scan",
                               text: "Nothing is captured until you start a scan. Keep faces, papers and screens out of the frame.")
                }
            }
            .padding(.horizontal, Spacing.gutter + 4)
            .padding(.top, Spacing.xxl)
            .padding(.bottom, Spacing.xl)
            .frame(maxWidth: 600, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollBounceBehavior(.basedOnSize)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: Spacing.s) {
                if let onAnswer {
                    Button("Agree and continue") { Consent.accept(); onAnswer(true) }
                        .buttonStyle(.primary)
                    Button("Not now") { onAnswer(false) }
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 44)
                } else {
                    Button("Done") { dismiss() }.buttonStyle(.primary)
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .padding(.top, Spacing.m)
            .padding(.bottom, Spacing.s)
            .background(Palette.canvas)
        }
        .background(Palette.canvas.ignoresSafeArea())
        .interactiveDismissDisabled(onAnswer != nil)
    }
}

private struct ConsentRow: View {
    let symbol: String
    let tone: Tone
    let title: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: Spacing.l) {
            IconBadge(symbol: symbol, tone: tone, filled: false)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(text).font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

#Preview { ConsentView(onAnswer: { _ in }) }
