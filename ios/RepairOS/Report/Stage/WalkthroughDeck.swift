import SwiftUI

/// The walkthrough's cards over the lower part of the stage, like a product page's feature slider: one card per step,
/// paging sideways, the active one a little larger. The pager's position drives the 3D camera (WalkthroughPlayer
/// `progress` → the hero's TourFrame), so a partial swipe turns the object partway and letting go settles on the
/// nearest card; while Lea narrates, the pager glides on by itself and the camera with it. Under it: a thin progress
/// line, back, pause or play, next; the close button ends the tour. The evidence photo opens full screen on a tap.
struct WalkthroughDeck: View {
    let player: WalkthroughPlayer
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.cropSource) private var cropSource
    @State private var selected: String?
    @State private var zoom: ZoomShas?

    var body: some View {
        VStack(spacing: Spacing.m) {
            header
            pager
            WalkthroughProgress(player: player).padding(.horizontal, Spacing.gutter + Spacing.m)
            controls
        }
        .environment(\.colorScheme, .dark)
        .onAppear { selected = player.step?.id }
        .onChange(of: player.index) { _, i in
            // Lea moved on (or back / next): glide there, the camera with it; a cut under Reduce Motion.
            guard player.steps.indices.contains(i), selected != player.steps[i].id else { return }
            withAnimation(reduceMotion ? nil : Motion.glide) { selected = player.steps[i].id }
        }
        .fullScreenCover(item: $zoom) { z in
            CropZoomView(shas: z.shas, index: 0).environment(\.cropSource, cropSource)
        }
    }

    private var header: some View {
        HStack {
            Label("Walkthrough", systemImage: "play.rectangle.fill")
                .font(.pillLabel).foregroundStyle(.white.opacity(0.8))
            Text("\(player.index + 1) of \(player.steps.count)")
                .font(.pillLabel).monospacedDigit().foregroundStyle(.white.opacity(0.55))
                .contentTransition(.numericText())
            Spacer()
            Button { player.stop() } label: { Image(systemName: Symbol.close) }
                .buttonStyle(StageIconButtonStyle())
                .accessibilityLabel("End the walkthrough")
        }
        .padding(.horizontal, Spacing.gutter)
        .accessibilityElement(children: .contain)
    }

    private var pager: some View {
        ScrollView(.horizontal) {
            LazyHStack(spacing: Spacing.m) {
                ForEach(Array(player.steps.enumerated()), id: \.element.id) { i, step in
                    WalkthroughCard(step: step, position: i + 1, count: player.steps.count) {
                        player.pause()
                        zoom = ZoomShas(shas: step.crops)
                    }
                    .containerRelativeFrame(.horizontal)
                    .scrollTransition(.interactive, axis: .horizontal) { content, phase in
                        content
                            .scaleEffect(phase.isIdentity ? 1 : 0.92)
                            .opacity(phase.isIdentity ? 1 : 0.55)
                    }
                    .id(step.id)
                }
            }
            .scrollTargetLayout()
        }
        .contentMargins(.horizontal, Spacing.gutter + Spacing.m, for: .scrollContent)
        .scrollTargetBehavior(.viewAligned)
        .scrollPosition(id: $selected, anchor: .center)
        .scrollIndicators(.hidden)
        .scrollClipDisabled()
        // The camera follows the finger: the pager's offset in cards, continuously.
        .onScrollGeometryChange(for: Double.self) { geo in
            let page = geo.containerSize.width - geo.contentInsets.leading - geo.contentInsets.trailing + Spacing.m
            return page > 1 ? (geo.contentOffset.x + geo.contentInsets.leading) / page : 0
        } action: { _, p in
            player.progress = p
        }
        // Let go: the card it settled on is the one Lea talks about.
        .onScrollPhaseChange { _, phase in
            guard phase == .idle, let id = selected, let i = player.steps.firstIndex(where: { $0.id == id }) else { return }
            player.go(to: i)
        }
        .frame(height: WalkthroughCard.height)
    }

    private var controls: some View {
        HStack(spacing: Spacing.l) {
            Button { player.back() } label: { Image(systemName: "chevron.left") }
                .buttonStyle(StageIconButtonStyle())
                .disabled(player.index == 0)
                .opacity(player.index == 0 ? 0.4 : 1)
                .accessibilityLabel("Previous step")
            Button { player.toggle() } label: {
                Image(systemName: player.playing ? "pause.fill" : (player.finished ? "arrow.counterclockwise" : "play.fill"))
                    .contentTransition(.symbolEffect(.replace))
            }
            .buttonStyle(StageIconButtonStyle())
            .accessibilityLabel(player.playing ? "Pause" : (player.finished ? "Play again" : "Resume"))
            Button { player.next() } label: { Image(systemName: "chevron.right") }
                .buttonStyle(StageIconButtonStyle())
                .disabled(player.index >= player.steps.count - 1)
                .opacity(player.index >= player.steps.count - 1 ? 0.4 : 1)
                .accessibilityLabel("Next step")
        }
        .sensoryFeedback(Haptic.select, trigger: player.index)
    }

    private struct ZoomShas: Identifiable {
        let shas: [String]
        var id: String { shas.joined() }
    }
}

/// The thin line under the cards: how far through the tour the pager is, following the swipe.
private struct WalkthroughProgress: View {
    let player: WalkthroughPlayer
    var body: some View {
        let n = max(1, player.steps.count - 1)
        Capsule().fill(Color.white.opacity(0.14))
            .overlay(alignment: .leading) {
                GeometryReader { g in
                    Capsule().fill(Palette.cameraAccent)
                        .frame(width: max(4, g.size.width * min(1, max(0, player.progress / Double(n)))))
                }
            }
            .frame(height: 3)
            .accessibilityHidden(true)
    }
}

/// One step: the eyebrow (with the problem's number), the title, Lea's line and the evidence photo.
struct WalkthroughCard: View {
    let step: Walkthrough.Step
    let position: Int
    let count: Int
    let zoom: () -> Void
    @Environment(\.accessibilityReduceTransparency) private var solid

    static let height: CGFloat = 176

    var body: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                HStack(spacing: 6) {
                    if let n = step.number { StageNumber(number: n, tone: step.tone) }
                    Text(step.eyebrow.uppercased()).font(.pillLabel).foregroundStyle(step.tone.color)
                        .lineLimit(1).minimumScaleFactor(0.75)
                }
                Text(step.title).font(.headline).foregroundStyle(.white).lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                // Lea's whole line, to read along (or alone when she is muted); a long one scrolls in the card.
                ScrollView(.vertical) {
                    Text(step.speech).font(.subheadline).foregroundStyle(.white.opacity(0.72))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
                .scrollIndicators(.hidden)
                // A soft edge where a long line runs on below.
                .mask(LinearGradient(stops: [.init(color: .black, location: 0.8), .init(color: .black.opacity(0.15), location: 1)],
                                     startPoint: .top, endPoint: .bottom))
            }
            Spacer(minLength: 0)
            if let sha = step.crops.first {
                Button(action: zoom) {
                    CropThumb(sha: sha, cornerRadius: Radius.thumb)
                        .frame(width: 76, height: 76)
                        .overlay(alignment: .bottomTrailing) {
                            Image(systemName: "arrow.up.left.and.arrow.down.right").font(.caption2.weight(.bold))
                                .foregroundStyle(.white).padding(5).background(.black.opacity(0.5), in: Circle()).padding(4)
                        }
                }
                .buttonStyle(PressableCardStyle())
                .accessibilityLabel("Evidence photo")
                .accessibilityHint("Opens the photo full screen")
            }
        }
        .padding(Spacing.l)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(solid ? Palette.stageGlassSolid : Color.black.opacity(0.55),
                    in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(position) of \(count). \(step.eyebrow). \(step.title). \(step.speech)")
    }
}

/// Reduce Motion: the hero dips out and back in around a change of card (a cross-fade and a cut, never a flight).
/// `shown` is the card the hero shows; it changes while the hero is faded out.
struct TourCutFade: ViewModifier {
    let nearest: Int?
    let on: Bool
    @Binding var shown: Int
    @State private var dipped = false

    func body(content: Content) -> some View {
        content
            .opacity(dipped ? 0 : 1)
            .onChange(of: nearest) { _, n in
                guard let n else { return }
                guard on else { shown = n; return }
                Task {
                    withAnimation(Motion.fade) { dipped = true }
                    try? await Task.sleep(for: .milliseconds(200))
                    shown = n
                    withAnimation(Motion.fade) { dipped = false }
                }
            }
    }
}

extension TourFrame {
    /// The frame the hero draws: under Reduce Motion, the card it shows (`shown`) as a cut, no blend.
    func drawn(reduceMotion: Bool, shown: Int) -> TourFrame {
        guard reduceMotion else { return self }
        var f = self
        f.progress = Double(shown)
        f.cut = true
        return f
    }
}
