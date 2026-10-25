import SwiftUI
import UIKit

// Overlay pieces for the live capture screen. Rules they follow (see Design/INTEGRATION.md):
// - Driven by coarse state only (cue words, cell states), at most ~10 changes a second. Nothing here reads ARFrame
//   or runs a per-frame timer; an animation runs only for the ~0.3 s after a change.
// - Chalk with keylines on smoke: no materials, blur or shadows over the camera. The dial is flattened with
//   `.drawingGroup()` into one Metal layer.
// - Under Reduce Motion every movement becomes a fade.

// MARK: - Cue banner

/// One short line of guidance with an icon, tinted by urgency: chalk words on a smoke capsule, a tone disc with a dark
/// glyph (the dark-mode status values, which read on smoke). Change `text` and it cross-fades.
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
            case .guide: Palette.next
            case .caution: Palette.cameraCaution
            case .stop: Palette.cameraDanger
            case .good: Palette.cameraGood
            }
        }

        var glyph: Color { Palette.cameraGlyph }
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
                .foregroundStyle(Palette.chalk)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
                .contentTransition(.opacity)
        }
        .padding(.leading, 6)
        .padding(.trailing, 18)
        .padding(.vertical, 6)
        .background { SmokeFill(shape: Capsule()) }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .motion(Motion.fade, value: text)
        .motion(Motion.fade, value: tone)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

// MARK: - Shot tick

/// A chalk check that pops in and fades out each time `trigger` changes (a photo was accepted), with the shot
/// haptic on the same frame. Invisible at rest; costs nothing between shots. Reduce Motion: an opacity blink only.
struct ShotTick: View {
    let trigger: Int
    var haptic = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Pop { var scale: CGFloat; var opacity: Double }

    var body: some View {
        Image(systemName: "checkmark")
            .font(.system(size: 24, weight: .bold))
            .foregroundStyle(Palette.cameraGlyph)
            .frame(width: 56, height: 56)
            .background(Circle().fill(Palette.chalk))
            .overlay(Circle().strokeBorder(Palette.keyline, lineWidth: 1))
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
                    LinearKeyframe(1, duration: reduceMotion ? 0.04 : 0.55)
                    LinearKeyframe(0, duration: reduceMotion ? 0.08 : 0.22)
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

/// The coverage dome seen from above, drawn as an astrolabe: 3 rings × 12 sectors = 36 cells inside a rim with 12
/// ticks and a mark at sector 0. `cells[ring * 12 + sector]`, ring 0 the lowest band (outer ring) up to ring 2 the
/// view from above (inner ring); sector 0 starts at the bottom of the dial (+Z) and runs toward +X (to the right), as
/// in `CoverageDome.cell(camera:box:)`. Empty cells are 1 pt chalk outlines, covered cells chalk, the next cell
/// `next` with an index tick on the rim beside it; `done` turns the covered cells leaf. `here` (the sector the
/// phone is in, already quantised) is a compass needle on the rim. `.camera` (default) sits on a smoke disc over the
/// live camera; `.app` draws the same astrolabe in `rule` and `accent` on an app screen.
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

    private var nextSector: Int? { cells.firstIndex(of: .next).map { $0 % Self.sectors } }

    var body: some View {
        ZStack {
            if style == .camera { Circle().fill(Palette.smoke) }
            Rim(sectors: Self.sectors).stroke(rimColor, lineWidth: Line.hairline + (style == .camera ? 0.5 : 0.25))
            ForEach(0..<Self.cellCount, id: \.self) { i in
                let state = i < cells.count ? cells[i] : .missing
                DomeCell(ring: i / Self.sectors, sector: i % Self.sectors)
                    .fill(fill(state))
                    .overlay(DomeCell(ring: i / Self.sectors, sector: i % Self.sectors)
                        .stroke(edge(state), lineWidth: Line.construction))
            }
            if let nextSector {
                RimTick(sector: nextSector).stroke(style == .camera ? Palette.next : Palette.accent,
                                                   style: StrokeStyle(lineWidth: Line.locked, lineCap: .butt))
            }
            if let here {
                HereNeedle(sector: here).fill(style == .camera ? Palette.chalk : Palette.ink)
            }
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

    private var rimColor: Color { style == .camera ? Palette.chalk.opacity(0.7) : Palette.rule.opacity(0.55) }

    private func fill(_ state: CellState) -> Color {
        switch (style, state) {
        case (_, .missing): .clear
        case (.camera, .covered): done ? Palette.cameraGood : Palette.chalk.opacity(0.85)
        case (.camera, .next): Palette.next
        case (.app, .covered): done ? Palette.good : Palette.accent.opacity(0.5)
        case (.app, .next): Palette.accent
        }
    }

    private func edge(_ state: CellState) -> Color {
        switch (style, state) {
        case (.camera, .missing): Palette.chalk.opacity(0.55)
        case (.app, .missing): Palette.rule.opacity(0.35)
        default: .clear
        }
    }
}

/// Where the cells sit: rings drawn outer (ring 0) to inner (ring 2), inside the rim.
private enum DomeGeometry {
    static func rim(_ rect: CGRect) -> CGFloat { rect.width / 2 - rect.width * 0.035 }
    static func outerCell(_ rect: CGRect) -> CGFloat { rect.width / 2 - rect.width * 0.12 }
    static func point(_ c: CGPoint, _ r: CGFloat, _ a: Double) -> CGPoint {
        // Top-down map: azimuth 0 at the bottom (+Z), increasing toward +X (right). Screen y grows downward.
        CGPoint(x: c.x + r * sin(a), y: c.y + r * cos(a))
    }
}

/// The rim: a circle with 12 short ticks pointing in, and a longer mark at sector 0.
private struct Rim: Shape {
    let sectors: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let r = DomeGeometry.rim(rect)
        var p = Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
        for s in 0..<sectors {
            let a = Double(s) * 2 * .pi / Double(sectors)
            let len = rect.width * (s == 0 ? 0.07 : 0.035)
            p.move(to: DomeGeometry.point(c, r, a))
            p.addLine(to: DomeGeometry.point(c, r - len, a))
        }
        return p
    }
}

/// One annular sector, with small radial and angular gaps.
private struct DomeCell: Shape {
    let ring: Int
    let sector: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let outer = DomeGeometry.outerCell(rect)
        let width = rect.width * 0.08
        let gap = rect.width * 0.025
        let r1 = outer - CGFloat(ring) * (width + gap)
        let r0 = r1 - width
        let span = 2 * Double.pi / Double(DomeDial.sectors)
        let pad = span * 0.07
        let a0 = Double(sector) * span + pad, a1 = Double(sector + 1) * span - pad
        var p = Path()
        p.move(to: DomeGeometry.point(c, r1, a0))
        let steps = 6
        for s in 1...steps { p.addLine(to: DomeGeometry.point(c, r1, a0 + (a1 - a0) * Double(s) / Double(steps))) }
        for s in stride(from: steps, through: 0, by: -1) { p.addLine(to: DomeGeometry.point(c, r0, a0 + (a1 - a0) * Double(s) / Double(steps))) }
        p.closeSubpath()
        return p
    }
}

/// The index tick on the rim beside the next cell (a shape cue, not only a colour).
private struct RimTick: Shape {
    let sector: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let a = (Double(sector) + 0.5) * 2 * .pi / Double(DomeDial.sectors)
        let r = DomeGeometry.rim(rect)
        var p = Path()
        p.move(to: DomeGeometry.point(c, r + rect.width * 0.02, a))
        p.addLine(to: DomeGeometry.point(c, r - rect.width * 0.06, a))
        return p
    }
}

/// A compass needle on the rim pointing at the centre: where the phone is.
private struct HereNeedle: Shape {
    let sector: Int

    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let a = (Double(sector) + 0.5) * 2 * .pi / Double(DomeDial.sectors)
        let r = DomeGeometry.rim(rect)
        let tip = r - rect.width * 0.09, tail = r + rect.width * 0.03, mid = r - rect.width * 0.02
        let half = rect.width * 0.022
        let dir = CGPoint(x: sin(a), y: cos(a)), side = CGPoint(x: cos(a), y: -sin(a))
        func at(_ radius: CGFloat, _ offset: CGFloat) -> CGPoint {
            CGPoint(x: c.x + dir.x * radius + side.x * offset, y: c.y + dir.y * radius + side.y * offset)
        }
        var p = Path()
        p.move(to: at(tip, 0))
        p.addLine(to: at(mid, half))
        p.addLine(to: at(tail, 0))
        p.addLine(to: at(mid, -half))
        p.closeSubpath()
        return p
    }
}

// MARK: - Lock box

/// How the box around the locked object looks, for either renderer: `LockBrackets` (SwiftUI, a projected 2D
/// rect) or RealityKit (a 3D box drawn with corner segments only).
enum LockBoxSpec {
    enum Phase: Sendable, Hashable {
        /// Looking for the object: thin chalk corners.
        case searching
        /// Locked: heavier `next` corners that settle in once (scale 1.06 → 1, `Motion.settle`) with `Haptic.lock`.
        case locked
    }

    /// Corner arm length as a fraction of the box's shorter edge, clamped to `cornerMin...cornerMax` points.
    static let cornerFraction: CGFloat = 0.22
    static let cornerMin: CGFloat = 14
    static let cornerMax: CGFloat = 34
    /// 2D stroke widths in points (over a keyline 2 pt wider); in 3D use a 1.5 mm (searching) / 2.5 mm (locked) tube
    /// or line, unlit.
    static func lineWidth(_ phase: Phase) -> CGFloat { phase == .locked ? Line.locked : Line.drawing }
    /// Unlit colours for RealityKit's `UnlitMaterial(color:)`; never lit, never with a shadow.
    static func uiColor(_ phase: Phase) -> UIColor {
        phase == .locked ? UIColor(hex: FixedHex.next) : UIColor(hex: FixedHex.chalk)
    }
    static func color(_ phase: Phase) -> Color { Color(uiColor: uiColor(phase)) }
    /// Scale the box starts at when it locks, before settling to 1.
    static let lockOvershoot: CGFloat = 1.06
}

/// Four open corners with square ends and mitred corners, filling the shape's frame (the lock brackets, a defect's
/// box on a photo).
struct CornerBrackets: Shape {
    func path(in rect: CGRect) -> Path {
        let arm = min(LockBoxSpec.cornerMax, max(LockBoxSpec.cornerMin, min(rect.width, rect.height) * LockBoxSpec.cornerFraction))
        var p = Path()
        for (corner, dx, dy) in [(CGPoint(x: rect.minX, y: rect.minY), 1.0, 1.0), (CGPoint(x: rect.maxX, y: rect.minY), -1.0, 1.0),
                                 (CGPoint(x: rect.maxX, y: rect.maxY), -1.0, -1.0), (CGPoint(x: rect.minX, y: rect.maxY), 1.0, -1.0)] {
            p.move(to: CGPoint(x: corner.x, y: corner.y + dy * arm))
            p.addLine(to: corner)
            p.addLine(to: CGPoint(x: corner.x + dx * arm, y: corner.y))
        }
        return p
    }
}

/// The lock brackets filling the view's frame: chalk 1.5 pt while searching, `next` 2.5 pt once locked, both over a
/// keyline. Place it over the object's projected bounds (update the frame at ≤ 10 Hz, not per frame). Lock: settles
/// once from 1.06 with `Haptic.lock`; Reduce Motion: the locked outline at once, same haptic.
struct LockBrackets: View {
    var phase: LockBoxSpec.Phase
    @State private var settle: CGFloat = 1
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Color.clear
            .keylined(CornerBrackets(), color: LockBoxSpec.color(phase), width: LockBoxSpec.lineWidth(phase))
            .scaleEffect(settle)
            .motion(Motion.fade, value: phase)
            .sensoryFeedback(Haptic.lock, trigger: phase) { _, new in new == .locked }
            .onChange(of: phase) { _, new in
                guard new == .locked, !reduceMotion else { return }
                settle = LockBoxSpec.lockOvershoot
                withAnimation(Motion.settle) { settle = 1 }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
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
    @State var locked = false
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
                    CueBanner(text: locked ? "Walk slowly around it" : "Point at the object",
                              symbol: locked ? "figure.walk" : Symbol.scan)
                    Button { locked.toggle(); shots += 1; if let i = cells.firstIndex(of: .next) { cells[i] = .covered; if i + 1 < 36 { cells[i + 1] = .next } } } label: {
                        Label(locked ? "Done" : "Scan", systemImage: locked ? "checkmark" : Symbol.scan)
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
