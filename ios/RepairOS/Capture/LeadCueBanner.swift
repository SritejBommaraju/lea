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
                // Same anatomy as CueBanner (icon disc + words on a dark capsule), in the accent: a lead, not a rule.
                HStack(spacing: 10) {
                    Image(systemName: "scope")
                        .font(.system(size: 14, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(Palette.cameraAccent.opacity(0.9), in: Circle())
                    VStack(alignment: .leading, spacing: 1) {
                        Text(shown.spoken).font(.cue).foregroundStyle(.white).lineLimit(2)
                        if shown.from == "web", let src = shown.sourceTitle, !src.isEmpty {
                            Text(src).font(.caption).foregroundStyle(.white.opacity(0.7)).lineLimit(1)
                        }
                    }
                }
                .padding(.leading, 6).padding(.trailing, 18).padding(.vertical, 6)
                .background(Color.black.opacity(0.58), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
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
