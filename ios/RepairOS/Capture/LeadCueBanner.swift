import SwiftUI

/// A live `lead` during the scan: where to take a close photo because of what the person said is wrong (their own
/// suspect first, then research leads). The area words come from the backend; the app only frames them. It shows
/// for a few seconds per new lead and never blocks the camera. A lead only changes where to look: the report's
/// findings still come from the photos.
struct LeadCueBanner: View {
    let lead: LiveEvents.Lead?
    @State private var shown: LiveEvents.Lead?

    var body: some View {
        Group {
            if let shown {
                // Same anatomy as CueBanner (a disc + words on a smoke capsule), with Lea's mark: it is Lea speaking,
                // a lead, not a rule.
                HStack(spacing: 10) {
                    LeaMarkView(size: 24, tint: Palette.next)
                        .frame(width: 30, height: 30)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(shown.spoken).font(.cue).foregroundStyle(Palette.chalk).lineLimit(2)
                        if shown.from == "web", let src = shown.sourceTitle, !src.isEmpty {
                            Text(src).font(.caption).foregroundStyle(Palette.chalk.opacity(0.7)).lineLimit(1)
                        }
                    }
                }
                .padding(.leading, 6).padding(.trailing, 18).padding(.vertical, 6)
                .background { SmokeFill(shape: Capsule()) }
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
                .transition(.opacity.combined(with: .move(edge: .top)))
                .accessibilityElement(children: .combine)
            }
        }
        .allowsHitTesting(false)
        .task(id: lead) {
            guard let lead else { return }
            withAnimation(Motion.standard) { shown = lead }
            try? await Task.sleep(for: .seconds(8))
            if shown == lead { withAnimation(Motion.standard) { shown = nil } }
        }
    }
}
