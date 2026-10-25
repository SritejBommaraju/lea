import SwiftUI

// The stage the scan sits on in the report (and while the report is being written): #12101B in both modes with a
// subtle studio backdrop (the owner, 2026-10-25), and a construction circle and ground grid in chalk (DESIGN §7.6).
// Static: drawn once and flattened, no blur, no material, nothing animated, so it costs nothing under the 3D view.
// Controls on the stage are chalk on smoke.

/// The stage: `Palette.stage` with a soft backdrop and the construction-circle ground. Fills its frame.
struct StageBackground: View {
    var body: some View {
        Palette.stage
            // A subtle studio backdrop (static): a touch lighter behind the object, falling to the stage colour.
            .overlay {
                RadialGradient(colors: [Color(fixed: 0x241F36), Palette.stage], center: UnitPoint(x: 0.5, y: 0.42),
                               startRadius: 0, endRadius: 360)
            }
            .overlay {
                Canvas(rendersAsynchronously: false) { ctx, size in
                    let chalk = GraphicsContext.Shading.color(Palette.chalk.opacity(0.35))
                    // The ground: an ellipse (a circle seen in perspective) low in the frame, with its diameters and
                    // a few receding grid lines.
                    let w = min(size.width * 0.78, 360), h = w * 0.22
                    let c = CGPoint(x: size.width / 2, y: size.height * 0.72)
                    let ground = CGRect(x: c.x - w / 2, y: c.y - h / 2, width: w, height: h)
                    ctx.stroke(Path(ellipseIn: ground), with: chalk, lineWidth: Line.construction)
                    ctx.stroke(Path(ellipseIn: ground.insetBy(dx: w * 0.18, dy: h * 0.18)), with: .color(Palette.chalk.opacity(0.18)),
                               lineWidth: Line.hairline)
                    var grid = Path()
                    grid.move(to: CGPoint(x: ground.minX, y: c.y)); grid.addLine(to: CGPoint(x: ground.maxX, y: c.y))
                    grid.move(to: CGPoint(x: c.x, y: ground.minY)); grid.addLine(to: CGPoint(x: c.x, y: ground.maxY))
                    for k in [-2.0, -1.0, 1.0, 2.0] {
                        let x = c.x + CGFloat(k) * w / 6
                        // Receding lines, converging slightly toward a far point above the ground.
                        grid.move(to: CGPoint(x: x + CGFloat(k) * w * 0.06, y: ground.maxY + h * 0.35))
                        grid.addLine(to: CGPoint(x: c.x + (x - c.x) * 0.55, y: ground.minY - h * 0.25))
                    }
                    ctx.stroke(grid, with: .color(Palette.chalk.opacity(0.18)), lineWidth: Line.hairline)
                }
            }
            .drawingGroup()
            .accessibilityHidden(true)
    }
}

/// A round 44 pt control on the stage: a chalk icon on smoke (solid under Reduce Transparency).
struct StageIconButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 17, weight: .regular))
            .foregroundStyle(Palette.chalk)
            .frame(width: 44, height: 44)
            .background { SmokeFill(shape: Circle()) }
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.92 : 1)
            .opacity(configuration.isPressed && reduceMotion ? 0.92 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// A capsule control on the stage ("Walk me through it"), chalk on smoke like `StageIconButtonStyle`.
struct StageCapsuleButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(Palette.chalk)
            .padding(.horizontal, 14)
            .frame(minHeight: 44)
            .background { SmokeFill(shape: Capsule()) }
            .contentShape(Capsule())
            .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.96 : 1)
            .opacity(configuration.isPressed && reduceMotion ? 0.92 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// One line of status on the stage ("Looking at photo 9 of 13"): chalk on a smoke capsule that reads over any scan.
/// The spinner turns only while `busy` and stops under Reduce Motion.
struct StageStatus: View {
    let text: String
    var busy = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var turn = false

    var body: some View {
        HStack(spacing: 10) {
            if busy {
                Circle().trim(from: 0, to: 0.7)
                    .stroke(Palette.chalk, style: StrokeStyle(lineWidth: Line.drawing, lineCap: .butt))
                    .frame(width: 16, height: 16)
                    .rotationEffect(.degrees(turn ? 360 : 0))
                    .onAppear {
                        guard !reduceMotion else { return }
                        withAnimation(.linear(duration: 0.9).repeatForever(autoreverses: false)) { turn = true }
                    }
                    .onChange(of: reduceMotion) { _, rm in if rm { withAnimation(nil) { turn = false } } }
                    .accessibilityHidden(true)
            }
            Text(text).font(.cue).monospacedDigit()
                .contentTransition(.numericText())
        }
        .foregroundStyle(Palette.chalk)
        .padding(.horizontal, 18).padding(.vertical, 12)
        .background { SmokeFill(shape: Capsule()) }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

/// A thin determinate progress line for the stage, in `next` (no orb, no spinner when the fraction is known).
struct StageProgressLine: View {
    let fraction: Double
    var body: some View {
        Capsule().fill(Palette.chalk.opacity(0.18))
            .overlay(alignment: .leading) {
                GeometryReader { g in
                    Capsule().fill(Palette.next).frame(width: max(4, g.size.width * min(1, max(0, fraction))))
                }
            }
            .frame(width: 220, height: 3)
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
            Button { } label: { Label("Walk me through it", systemImage: "play") }.buttonStyle(StageCapsuleButtonStyle())
            StageStatus(text: "Looking at photo 9 of 13")
            StageProgressLine(fraction: 0.6)
        }
        .padding()
    }
}
#endif
