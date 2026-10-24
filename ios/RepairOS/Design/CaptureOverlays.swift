import SwiftUI
import UIKit

// Overlay pieces for the live capture screen. Rules they follow (see Design/INTEGRATION.md):
// - Driven by coarse state only (cue words, cell states, counters), at most ~10 changes a second. Nothing here
//   reads ARFrame or runs a per-frame timer; an animation runs only for the ~0.3 s after a change.
// - No materials, blur or shadows: solid translucent fills over the camera. The dial is flattened with
//   `.drawingGroup()` into one Metal layer.
// - Under Reduce Motion every movement becomes a fade.

// MARK: - Cue banner

/// One short line of guidance with an icon, tinted by urgency. Change `text` and it cross-fades.
struct CueBanner: View {
    enum Tone: Sendable, Hashable {
        /// Ordinary guidance ("Walk slowly around it").
        case guide
        /// Fixable now ("Slow down", "More light").
        case caution
        /// Blocks capture ("Too close", "Object lost").
        case stop
        /// Something went right ("All sides covered").
        case good

        var fill: Color {
            switch self {
            case .guide: Color.white.opacity(0.18)
            case .caution: Color(uiColor: UIColor(hex: 0xFFB340))
            case .stop: Color(uiColor: UIColor(hex: 0xFF5C66))
            case .good: Color(uiColor: UIColor(hex: 0x3DDC84))
            }
        }

        var glyph: Color { self == .guide ? .white : Color(white: 0.06) }
    }

    let text: String
    let symbol: String
    var tone: Tone = .guide

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(tone.glyph)
                .contentTransition(.symbolEffect(.replace))
                .frame(width: 30, height: 30)
                .background(tone.fill, in: Circle())
            Text(text)
                .font(.cue)
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
                .contentTransition(.opacity)
        }
        .padding(.leading, 6)
        .padding(.trailing, 18)
        .padding(.vertical, 6)
        .background(Color.black.opacity(0.58), in: Capsule())
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .motion(Motion.fade, value: text)
        .motion(Motion.fade, value: tone)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

// MARK: - Shot tick

/// A white check that pops in and fades out each time `trigger` changes (a photo was accepted), with the shot
/// haptic on the same frame. Invisible at rest; costs nothing between shots.
struct ShotTick: View {
    let trigger: Int
    var haptic = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Pop { var scale: CGFloat; var opacity: Double }

    var body: some View {
        Image(systemName: "checkmark")
            .font(.system(size: 24, weight: .bold))
            .foregroundStyle(Color(white: 0.06))
            .frame(width: 56, height: 56)
            .background(Circle().fill(.white))
            .keyframeAnimator(initialValue: Pop(scale: reduceMotion ? 1 : 0.7, opacity: 0), trigger: trigger) { view, pop in
                view.scaleEffect(pop.scale).opacity(pop.opacity)
            } keyframes: { _ in
                KeyframeTrack(\.scale) {
                    SpringKeyframe(1, duration: 0.3, spring: .init(response: 0.3, dampingRatio: 0.7))
                    LinearKeyframe(1, duration: 0.35)
                    LinearKeyframe(reduceMotion ? 1 : 0.94, duration: 0.2)
                }
                KeyframeTrack(\.opacity) {
                    LinearKeyframe(1, duration: 0.08)
                    LinearKeyframe(1, duration: 0.55)
                    LinearKeyframe(0, duration: 0.22)
                }
            }
            .sensoryFeedback(haptic ? Haptic.shot : .selection, trigger: trigger) { _, _ in haptic }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

// MARK: - Coverage dial

/// One cell of the coverage dome.
enum CellState: Sendable, Hashable {
    /// No accepted photo from this direction yet.
    case missing
    /// An accepted photo covers it.
    case covered
    /// The cell the cue is steering toward.
    case next
}

/// The coverage dome seen from above: 3 rings × 12 sectors = 36 cells. `cells[ring * 12 + sector]`, ring 0 the
/// lowest band (outer ring) up to ring 2 the view from above (inner ring); sector 0 starts at the bottom of the
/// dial (+Z) and runs toward +X (to the right), as in `CoverageDome.cell(camera:box:)`. `here` is the sector the
/// phone is in, already quantised; `done` turns the covered cells green. `.camera` (default) is for the live
/// camera; `.app` draws it on an app screen (accent cells, no dark disc).
struct DomeDial: View {
    enum Style { case camera, app }
    nonisolated static let rings = 3
    nonisolated static let sectors = 12
    nonisolated static let cellCount = rings * sectors

    let cells: [CellState]
    var here: Int?
    var done = false
    var size: CGFloat = 76
    var style: Style = .camera

    var body: some View {
        ZStack {
            Circle().fill(style == .camera ? Color.black.opacity(0.45) : Palette.accent.opacity(0.07))
            ForEach(0..<Self.cellCount, id: \.self) { i in
                DomeCell(ring: i / Self.sectors, sector: i % Self.sectors)
                    .fill(color(i < cells.count ? cells[i] : .missing))
            }
            if let here {
                HereMarker(sector: here).fill(style == .camera ? Color.white : Color.primary)
            }
            Image(systemName: "checkmark")
                .font(.system(size: size * 0.16, weight: .heavy))
                .foregroundStyle(Palette.good)
                .opacity(done ? 1 : 0)
                .scaleEffect(done ? 1 : 0.6)
        }
        .frame(width: size, height: size)
        .drawingGroup()
        .motion(Motion.fade, value: cells)
        .motion(Motion.standard, value: here)
        .motion(Motion.settle, value: done)
        .accessibilityElement()
        .accessibilityLabel("Coverage")
        .accessibilityValue("\(cells.filter { $0 == .covered }.count) of \(Self.cellCount) sides")
    }

    private func color(_ state: CellState) -> Color {
        switch (style, state) {
        case (.camera, .missing): Color.white.opacity(0.2)
        case (.camera, .covered): done ? Palette.good : Color.white
        case (.camera, .next): Palette.cameraAccent
        case (.app, .missing): Palette.accent.opacity(0.14)
        case (.app, .covered): done ? Palette.good : Palette.accent
        case (.app, .next): Palette.accent.opacity(0.45)
        }
    }
}

/// One annular sector. Rings are drawn outer (ring 0) to inner (ring 2), with small radial and angular gaps.
private struct DomeCell: Shape {
    let ring: Int
    let sector: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let outer = rect.width / 2 - rect.width * 0.08
        let width = rect.width * 0.085
        let gap = rect.width * 0.025
        let r1 = outer - CGFloat(ring) * (width + gap)
        let r0 = r1 - width
        let span = 2 * Double.pi / Double(DomeDial.sectors)
        let pad = span * 0.06
        let a0 = Double(sector) * span + pad, a1 = Double(sector + 1) * span - pad
        // Top-down map: azimuth 0 at the bottom (+Z), increasing toward +X (right). Screen y grows downward.
        func point(_ r: CGFloat, _ a: Double) -> CGPoint { CGPoint(x: c.x + r * sin(a), y: c.y + r * cos(a)) }
        var p = Path()
        p.move(to: point(r1, a0))
        let steps = 6
        for s in 1...steps { p.addLine(to: point(r1, a0 + (a1 - a0) * Double(s) / Double(steps))) }
        for s in stride(from: steps, through: 0, by: -1) { p.addLine(to: point(r0, a0 + (a1 - a0) * Double(s) / Double(steps))) }
        p.closeSubpath()
        return p
    }
}

/// A small triangle on the rim pointing at the centre: where the phone is.
private struct HereMarker: Shape {
    let sector: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let a = (Double(sector) + 0.5) * 2 * .pi / Double(DomeDial.sectors)
        let tip = rect.width / 2 - rect.width * 0.035, base = rect.width / 2 + rect.width * 0.0
        let half = 0.09
        func point(_ r: CGFloat, _ a: Double) -> CGPoint { CGPoint(x: c.x + r * sin(a), y: c.y + r * cos(a)) }
        var p = Path()
        p.move(to: point(tip, a))
        p.addLine(to: point(base, a - half))
        p.addLine(to: point(base, a + half))
        p.closeSubpath()
        return p
    }
}

// MARK: - Lock box

/// How the box around the locked object looks, for either renderer: `LockBrackets` (SwiftUI, a projected 2D
/// rect) or RealityKit (a 3D box drawn with corner segments only).
enum LockBoxSpec {
    enum Phase: Sendable, Hashable {
        /// Looking for the object: thin white corners.
        case searching
        /// Locked: thicker accent corners that settle in once (scale 1.06 → 1, `Motion.settle`) with `Haptic.lock`.
        case locked
    }

    /// Corner arm length as a fraction of the box's shorter edge, clamped to `cornerMin...cornerMax` points.
    static let cornerFraction: CGFloat = 0.22
    static let cornerMin: CGFloat = 14
    static let cornerMax: CGFloat = 34
    /// 2D stroke widths in points; in 3D use a 1.5 mm (searching) / 2.5 mm (locked) tube or line, unlit.
    static func lineWidth(_ phase: Phase) -> CGFloat { phase == .locked ? 4 : 2.5 }
    /// Unlit colours for RealityKit's `UnlitMaterial(color:)`; never lit, never with a shadow.
    static func uiColor(_ phase: Phase) -> UIColor {
        phase == .locked ? UIColor(hex: 0x9C94FF) : UIColor(white: 1, alpha: 0.8)
    }
    static func color(_ phase: Phase) -> Color { Color(uiColor: uiColor(phase)) }
    /// Scale the box starts at when it locks, before settling to 1.
    static let lockOvershoot: CGFloat = 1.06
}

/// Four rounded corner brackets filling the view's frame. Place it over the object's projected bounds (update the
/// frame at ≤ 10 Hz, not per frame).
struct LockBrackets: View {
    var phase: LockBoxSpec.Phase
    @State private var settle: CGFloat = 1

    var body: some View {
        Brackets()
            .stroke(LockBoxSpec.color(phase),
                    style: StrokeStyle(lineWidth: LockBoxSpec.lineWidth(phase), lineCap: .round, lineJoin: .round))
            .scaleEffect(settle)
            .motion(Motion.fade, value: phase)
            .sensoryFeedback(Haptic.lock, trigger: phase) { _, new in new == .locked }
            .onChange(of: phase) { _, new in
                guard new == .locked else { return }
                settle = LockBoxSpec.lockOvershoot
                withAnimation(Motion.settle) { settle = 1 }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    private struct Brackets: Shape {
        func path(in rect: CGRect) -> Path {
            let arm = min(LockBoxSpec.cornerMax, max(LockBoxSpec.cornerMin, min(rect.width, rect.height) * LockBoxSpec.cornerFraction))
            let r = min(arm * 0.45, 10)
            var p = Path()
            for (corner, dx, dy) in [(CGPoint(x: rect.minX, y: rect.minY), 1.0, 1.0), (CGPoint(x: rect.maxX, y: rect.minY), -1.0, 1.0),
                                     (CGPoint(x: rect.maxX, y: rect.maxY), -1.0, -1.0), (CGPoint(x: rect.minX, y: rect.maxY), 1.0, -1.0)] {
                p.move(to: CGPoint(x: corner.x, y: corner.y + dy * arm))
                p.addLine(to: CGPoint(x: corner.x, y: corner.y + dy * r))
                p.addQuadCurve(to: CGPoint(x: corner.x + dx * r, y: corner.y), control: corner)
                p.addLine(to: CGPoint(x: corner.x + dx * arm, y: corner.y))
            }
            return p
        }
    }
}

// MARK: - Previews

#if DEBUG
/// A stand-in camera picture for previews: a dark, uneven gradient (static; previews only).
struct CameraBackdrop: View {
    var image: UIImage?
    var body: some View {
        if let image {
            Color.black.overlay { Image(uiImage: image).resizable().scaledToFill() }.clipped().ignoresSafeArea()
        } else {
            LinearGradient(colors: [Color(white: 0.32), Color(white: 0.12), Color(white: 0.22)], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
        }
    }
}

/// The capture screen's overlay layout as INTEGRATION.md describes it, with sample states.
struct CaptureOverlayPreview: View {
    var backdrop: UIImage?
    @State private var shots = 0
    @State private var locked = false
    @State private var cells: [CellState] = (0..<36).map { [1, 2, 3, 13, 14, 15, 25, 26].contains($0) ? .covered : ($0 == 4 ? .next : .missing) }

    var body: some View {
        ZStack {
            CameraBackdrop(image: backdrop)
            LockBrackets(phase: locked ? .locked : .searching)
                .frame(width: 210, height: 230)
            ShotTick(trigger: shots)
            VStack {
                HStack(alignment: .top) {
                    Button { } label: { Image(systemName: Symbol.close) }.buttonStyle(RoundIconButtonStyle(onCamera: true))
                    Spacer()
                    DomeDial(cells: cells, here: 3)
                }
                Spacer()
                VStack(spacing: Spacing.m) {
                    CueBanner(text: locked ? "Walk slowly around it" : "Tap the object to lock on",
                              symbol: locked ? "figure.walk" : Symbol.lock)
                    HStack(spacing: Spacing.s) {
                        StatusPill(text: "\(shots + 8) photos", symbol: "camera.fill", onCamera: true)
                        ProgressPill(text: "Uploading", fraction: 0.35, onCamera: true)
                    }
                    Button { locked.toggle(); shots += 1; if let i = cells.firstIndex(of: .next) { cells[i] = .covered; if i + 1 < 36 { cells[i + 1] = .next } } } label: {
                        Label(locked ? "Close-ups" : "Lock on", systemImage: locked ? Symbol.closeup : Symbol.lock)
                    }
                    .buttonStyle(.primary(.light))
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
    }
}

#Preview("Capture overlay") { CaptureOverlayPreview() }

#Preview("Cue tones") {
    VStack(spacing: 12) {
        CueBanner(text: "Walk slowly around it", symbol: "figure.walk")
        CueBanner(text: "Slow down", symbol: "tortoise.fill", tone: .caution)
        CueBanner(text: "Too close", symbol: "arrow.down.backward.and.arrow.up.forward", tone: .stop)
        CueBanner(text: "All sides covered", symbol: "checkmark", tone: .good)
    }
    .padding()
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(CameraBackdrop())
}

#Preview("Dome dial") {
    HStack(spacing: 20) {
        DomeDial(cells: Array(repeating: .missing, count: 36))
        DomeDial(cells: (0..<36).map { $0 % 3 == 0 ? .covered : ($0 == 7 ? .next : .missing) }, here: 7)
        DomeDial(cells: Array(repeating: .covered, count: 36), done: true)
    }
    .padding()
    .background(CameraBackdrop())
}
#endif
