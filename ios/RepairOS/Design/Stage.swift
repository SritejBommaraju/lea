import SwiftUI

// The dark stage the scan sits on in the report (and while the report is being written): the same near-black as
// the camera and the big-screen viewer, so the cloud reads as light rather than dust. Static views only: no blur,
// no material, nothing animated, so it costs nothing under the 3D view drawn on top of it.

/// The stage: a radial gradient with a faint accent floor under the object. Fills its frame.
struct StageBackground: View {
    var body: some View {
        ZStack {
            RadialGradient(colors: [Palette.stageCenter, Palette.stageEdge], center: UnitPoint(x: 0.5, y: 0.55),
                           startRadius: 8, endRadius: 420)
            // The object sits on something; a soft accent ellipse low in the frame, drawn as a gradient (no blur).
            EllipticalGradient(colors: [Palette.cameraAccent.opacity(0.12), .clear], center: .center,
                               startRadiusFraction: 0, endRadiusFraction: 0.5)
                .frame(width: 320, height: 60)
                .frame(maxHeight: .infinity, alignment: .bottom)
                .padding(.bottom, 56)
        }
        .accessibilityHidden(true)
    }
}

/// A round 44 pt control on the stage: white glass (white at 12%, hairline edge), solid under Reduce Transparency.
struct StageIconButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceTransparency) private var solid

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: 44, height: 44)
            .background(solid ? Palette.stageGlassSolid : Palette.stageGlass, in: Circle())
            .overlay(Circle().strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// A capsule control on the stage ("Show on object", "+3"), same glass as `StageIconButtonStyle`.
struct StageCapsuleButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceTransparency) private var solid

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 14)
            .frame(minHeight: 40)
            .background(solid ? Palette.stageGlassSolid : Palette.stageGlass, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.16), lineWidth: 0.5))
            .contentShape(Capsule())
            .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// One line of status on the stage ("Looking at photo 9 of 13"): a dark capsule that reads over any cloud.
struct StageStatus: View {
    let text: String
    var busy = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var turn = false

    var body: some View {
        HStack(spacing: 10) {
            if busy {
                Circle().trim(from: 0, to: 0.7)
                    .stroke(Color.white, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                    .frame(width: 16, height: 16)
                    .rotationEffect(.degrees(turn ? 360 : 0))
                    .onAppear {
                        guard !reduceMotion else { return }
                        withAnimation(.linear(duration: 0.9).repeatForever(autoreverses: false)) { turn = true }
                    }
                    .accessibilityHidden(true)
            }
            Text(text).font(.system(.callout, design: .rounded, weight: .semibold)).monospacedDigit()
                .contentTransition(.numericText())
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 18).padding(.vertical, 12)
        .background(Color.black.opacity(0.55), in: Capsule())
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

/// A thin determinate progress line for the stage (no orb, no spinner when the fraction is known).
struct StageProgressLine: View {
    let fraction: Double
    var body: some View {
        Capsule().fill(Color.white.opacity(0.14))
            .overlay(alignment: .leading) {
                GeometryReader { g in
                    Capsule().fill(Palette.cameraAccent).frame(width: max(4, g.size.width * min(1, max(0, fraction))))
                }
            }
            .frame(width: 220, height: 4)
            .motion(Motion.standard, value: fraction)
            .accessibilityHidden(true)
    }
}

#if DEBUG
#Preview("Stage") {
    ZStack {
        StageBackground().ignoresSafeArea()
        VStack(spacing: 16) {
            HStack {
                Button { } label: { Image(systemName: Symbol.close) }.buttonStyle(StageIconButtonStyle())
                Spacer()
                Button { } label: { Image(systemName: "ellipsis") }.buttonStyle(StageIconButtonStyle())
            }
            Spacer()
            Button { } label: { Label("Show on object", systemImage: "arkit") }.buttonStyle(StageCapsuleButtonStyle())
            StageStatus(text: "Looking at photo 9 of 13")
            StageProgressLine(fraction: 0.6)
        }
        .padding()
    }
}
#endif
