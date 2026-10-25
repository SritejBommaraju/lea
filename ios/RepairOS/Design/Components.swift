import SwiftUI
import UIKit

// Shared building blocks for every screen. Press feedback is on touch-down (ButtonStyle.isPressed) and turns into an
// opacity change under Reduce Motion; springs come from Motion, colours from Palette/Tone. Only `GlassBar` uses a
// backdrop material (one floating bar per screen); glass cards are the same look without the blur, and nothing here
// puts glass or blur over the camera or the stage (those use smoke).

// MARK: - Buttons

/// The one big action on a screen: a 56 pt capsule. `.accent` (blue, onAccent label) on app screens, `.light`
/// (chalk, ink label) over the live camera, `.quiet` (a glass capsule, ink label) for the secondary action beside it,
/// `.danger` (danger fill) only for deleting.
struct PrimaryButtonStyle: ButtonStyle {
    enum Kind { case accent, light, quiet, danger }
    var kind: Kind = .accent
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.buttonLabel)
            .labelStyle(.titleAndIcon)
            .foregroundStyle(foreground)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .padding(.horizontal, Spacing.xl)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background { background }
            .contentShape(Capsule())
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.97 : 1)
            .opacity(isEnabled ? (configuration.isPressed ? 0.92 : 1) : 0.4)
            .animation(Motion.press, value: configuration.isPressed)
    }

    private var foreground: Color {
        switch kind {
        case .accent: Palette.onAccent
        case .light: Color(fixed: FixedHex.inkLight)
        case .quiet: Palette.ink
        case .danger: Palette.onDanger
        }
    }

    @ViewBuilder private var background: some View {
        switch kind {
        case .accent: Capsule().fill(Palette.accent)
        case .light: Capsule().fill(Palette.chalk).overlay(Capsule().strokeBorder(Palette.keyline, lineWidth: Line.hairline))
        case .quiet: Color.clear.glass(.card, in: Capsule())
        case .danger: Capsule().fill(Palette.danger)
        }
    }
}

extension ButtonStyle where Self == PrimaryButtonStyle {
    static var primary: PrimaryButtonStyle { PrimaryButtonStyle() }
    static func primary(_ kind: PrimaryButtonStyle.Kind) -> PrimaryButtonStyle { PrimaryButtonStyle(kind: kind) }
}

/// A 44 pt round icon button (close, settings). App screens: an outline icon on a glass disc. `onCamera`: a smoke
/// disc with a chalk icon (no material over the camera).
struct RoundIconButtonStyle: ButtonStyle {
    var onCamera = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 17, weight: .regular))
            .foregroundStyle(onCamera ? Palette.chalk : Palette.ink)
            .frame(width: 44, height: 44)
            .background {
                if onCamera {
                    SmokeFill(shape: Circle())
                } else {
                    Color.clear.glass(.card, in: Circle())
                }
            }
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.92 : 1)
            .opacity(configuration.isPressed && reduceMotion ? 0.92 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// Makes a whole card or row pressable with the same touch-down feedback as the buttons.
struct PressableCardStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.9 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// A plain text button in the tint ("Not now", "Skip"): 44 pt tall, never a second capsule.
struct TextActionStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(Palette.accent)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}

extension ButtonStyle where Self == TextActionStyle {
    static var textAction: TextActionStyle { TextActionStyle() }
}

// MARK: - Glass

/// The shared frosted treatment (DESIGN §3): `glassFill` with the `glassEdge`, a 1 pt inner top highlight (white at
/// 22%) and a soft navy shadow. `.bar` adds the backdrop blur and is for the one floating bar per screen; `.card` is
/// the same look without a backdrop (cards, quiet capsules, round buttons on the canvas). Reduce Transparency:
/// opaque `surface` with a hairline, no blur, no shadow.
enum GlassKind: Sendable { case bar, card }

private struct GlassModifier<S: InsettableShape>: ViewModifier {
    let kind: GlassKind
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var solid

    func body(content: Content) -> some View {
        content.background {
            if solid {
                shape.fill(Palette.surface).overlay(shape.strokeBorder(Palette.hairline, lineWidth: Line.hairline))
            } else {
                ZStack {
                    if kind == .bar { shape.fill(.ultraThinMaterial) }
                    shape.fill(Palette.glassFill)
                }
                .overlay(shape.strokeBorder(Palette.glassEdge, lineWidth: Line.hairline))
                .overlay(shape.strokeBorder(LinearGradient(colors: [Color.white.opacity(0.22), .clear], startPoint: .top,
                                                           endPoint: UnitPoint(x: 0.5, y: 0.12)), lineWidth: 1))
                .shadow(color: Palette.glassShadow.opacity(0.07), radius: 18, y: 6)
            }
        }
    }
}

extension View {
    /// The frosted glass behind this view, in `shape`.
    func glass<S: InsettableShape>(_ kind: GlassKind, in shape: S) -> some View {
        modifier(GlassModifier(kind: kind, shape: shape))
    }
}

/// The floating bottom bar of a screen (Home's talk bar, Consent and Pairing, the report's pinned Ask Lea bar): the
/// one backdrop material on the screen, a glass edge along its top, extending under the home indicator. Place it
/// with `.safeAreaInset(edge: .bottom) { GlassBar { … } }`.
struct GlassBar<Content: View>: View {
    @ViewBuilder var content: Content
    @Environment(\.accessibilityReduceTransparency) private var solid

    var body: some View {
        VStack(spacing: Spacing.s) { content }
            .padding(.horizontal, Spacing.gutter)
            .padding(.top, Spacing.m)
            .padding(.bottom, Spacing.s)
            .frame(maxWidth: .infinity)
            .background {
                ZStack {
                    if solid {
                        Palette.surface
                    } else {
                        Rectangle().fill(.ultraThinMaterial)
                        Palette.glassFill
                    }
                }
                .overlay(alignment: .top) {
                    Rectangle().fill(solid ? Palette.hairline : Palette.glassEdge).frame(height: Line.hairline)
                }
                .ignoresSafeArea(edges: .bottom)
            }
    }
}

// MARK: - Smoke (over the camera and the stage)

/// A smoke fill with a faint chalk edge, solid under Reduce Transparency. The camera and the stage use this instead
/// of any material.
struct SmokeFill<S: InsettableShape>: View {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var solid

    var body: some View {
        shape.fill(solid ? Palette.smokeSolid : Palette.smoke)
            .overlay(shape.strokeBorder(Palette.chalk.opacity(0.16), lineWidth: Line.hairline))
    }
}

private struct CameraChipModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.pillLabel)
            .foregroundStyle(Palette.chalk)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background { SmokeFill(shape: Capsule()) }
            .dynamicTypeSize(...DynamicTypeSize.accessibility1)
    }
}

extension View {
    /// A chip over the camera or the stage ("Looks like: …", "Scan the bottom", "Lea is looking"): chalk on smoke.
    func cameraChip() -> some View { modifier(CameraChipModifier()) }

    /// Draws `stroke` in chalk (or `color`) over a keyline 2 pt wider, so a line over the camera survives any scene.
    func keylined<S: Shape>(_ shape: S, color: Color = Palette.chalk, width: CGFloat = Line.drawing,
                            style: StrokeStyle? = nil) -> some View {
        let base = style ?? StrokeStyle(lineWidth: width, lineCap: .butt, lineJoin: .miter)
        var under = base
        under.lineWidth = base.lineWidth + 2
        return overlay {
            ZStack {
                shape.stroke(Palette.keyline, style: under)
                shape.stroke(color, style: base)
            }
        }
    }
}

// MARK: - Surfaces

private struct CardModifier: ViewModifier {
    var padding: CGFloat
    var tone: Tone?

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                let shape = RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                ZStack {
                    shape.fill(Palette.surface)
                    if let tone { shape.fill(tone.wash) }
                }
                .overlay(shape.strokeBorder(tone.map { $0.color.opacity(0.28) } ?? Palette.hairline, lineWidth: Line.hairline))
            }
    }
}

extension View {
    /// An opaque reading surface on the canvas: radius 12, `surface` and a hairline. `tone` is for safety only.
    func card(padding: CGFloat = Spacing.l, tone: Tone? = nil) -> some View {
        modifier(CardModifier(padding: padding, tone: tone))
    }
}

/// A section heading on a scrolling screen: `sectionTitle`, optional count, optional trailing accessory.
struct SectionHeader<Trailing: View>: View {
    let title: String
    var count: Int?
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Text(title).display(.section).foregroundStyle(Palette.ink)
            if let count {
                Text("\(count)").font(.label).monospacedDigit().foregroundStyle(Palette.secondary)
            }
            Spacer(minLength: 0)
            trailing
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

extension SectionHeader where Trailing == EmptyView {
    init(_ title: String, count: Int? = nil) {
        self.init(title: title, count: count) { EmptyView() }
    }
}

/// A radius-12 `surface` group with hairline dividers between its rows (each at least 44 pt): Fix it offers, Also
/// worth a look, What wasn't checked, the full report link, consent.
struct GroupedList<Content: View>: View {
    var inset: CGFloat = Spacing.l
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group(subviews: content) { rows in
                ForEach(Array(rows.enumerated()), id: \.offset) { i, row in
                    if i > 0 { Rectangle().fill(Palette.hairline).frame(height: Line.hairline).padding(.leading, inset) }
                    row.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .padding(.horizontal, inset)
                }
            }
        }
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }
}

/// Label and value rows separated by 0.5 pt rules: the label in `secondary` at a fixed 96 pt column, the value in
/// `ink`. Used by the problem card, the problem detail and the brief card.
struct RuledRows: View {
    struct Row: Identifiable, Sendable {
        var id: String { label + "|" + value }
        let label: String
        let value: String
        init(_ label: String, _ value: String) { self.label = label; self.value = value }
    }

    let rows: [Row]
    @Environment(\.dynamicTypeSize) private var typeSize
    @ScaledMetric(relativeTo: .footnote) private var labelWidth: CGFloat = 96

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(rows) { row in
                Rectangle().fill(Palette.hairline).frame(height: Line.hairline)
                let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 2))
                    : AnyLayout(HStackLayout(alignment: .firstTextBaseline, spacing: Spacing.m))
                layout {
                    Text(row.label).font(.label).foregroundStyle(Palette.secondary)
                        .frame(width: typeSize.isAccessibilitySize ? nil : labelWidth, alignment: .leading)
                    Text(row.value).font(.subheadline).foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.vertical, Spacing.s)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

// MARK: - Labels

/// A read-only status: words and an icon in the tone, no fill (a status is never a button). `dashed` outlines it with
/// a dashed capsule: unknown only. Safety is always `exclamationmark.triangle` in danger.
struct ToneTag: View {
    let text: String
    var tone: Tone = .neutral
    var symbol: String?
    var dashed = false

    var body: some View {
        HStack(spacing: 4) {
            if let symbol { Image(systemName: symbol).imageScale(.small).accessibilityHidden(true) }
            Text(text).lineLimit(1)
        }
        .font(.label)
        .foregroundStyle(tone.color)
        .padding(.horizontal, dashed ? 7 : 0)
        .padding(.vertical, dashed ? 3 : 0)
        .overlay {
            if dashed {
                Capsule().strokeBorder(tone.color.opacity(0.8), style: StrokeStyle(lineWidth: Line.hairline, dash: Line.dash))
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// The tag for a problem's severity: safety with the warning symbol, cosmetic and functional in words.
    static func severity(_ s: ReportModel.Severity?) -> ToneTag? {
        guard let s else { return nil }
        switch s {
        case .safety: return ToneTag(text: "Safety risk", tone: .danger, symbol: Symbol.safety)
        case .functional: return ToneTag(text: "Functional issue", tone: .caution)
        case .cosmetic: return ToneTag(text: "Cosmetic", tone: .cosmetic)
        }
    }
}

/// A short status: icon + words in the tone (a `ToneTag`). `onCamera` draws chalk on a smoke capsule for use over AR.
struct StatusPill: View {
    let text: String
    var symbol: String?
    var tone: Tone = .neutral
    var onCamera = false

    var body: some View {
        if onCamera {
            HStack(spacing: 5) {
                if let symbol { Image(systemName: symbol).imageScale(.small) }
                Text(text).lineLimit(1)
            }
            .cameraChip()
            .accessibilityElement(children: .combine)
        } else {
            ToneTag(text: text, tone: tone, symbol: symbol)
        }
    }
}

/// An input row (symptom, gateway fields): icon + field in a `well`.
struct FieldWell<Field: View>: View {
    let symbol: String
    @ViewBuilder var field: Field

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: symbol).foregroundStyle(Palette.secondary).accessibilityHidden(true)
            field.font(.body).foregroundStyle(Palette.ink)
        }
        .padding(.horizontal, Spacing.l)
        .frame(minHeight: 52)
        .background(Palette.well, in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
    }
}

/// A 36 pt dashed rounded square with a symbol: the row icon for anything not looked at or not clear enough.
struct DashedTile: View {
    let symbol: String

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 15, weight: .regular))
            .foregroundStyle(Palette.secondary)
            .frame(width: 36, height: 36)
            .overlay(RoundedRectangle(cornerRadius: Radius.small, style: .continuous)
                .strokeBorder(Palette.secondary.opacity(0.7), style: StrokeStyle(lineWidth: Line.hairline + 0.5, dash: Line.dash)))
            .accessibilityHidden(true)
    }
}

// MARK: - Marker

/// A problem's number, the same mark everywhere (the card, the 3D stage, the photo leader, AR): a disc on `surface`
/// outlined 1.5 pt in the severity tone, the number in the display face in the tone. `onStage` uses the dark surface
/// and dark tone values whatever the phone's mode, so it reads on the stage and the camera.
struct Marker: View {
    let number: Int
    var tone: Tone
    var size: CGFloat = 26
    var onStage = false
    @Environment(\.legibilityWeight) private var weight

    var body: some View {
        let colour = onStage ? tone.cameraColor : tone.color
        Text("\(number)")
            .font(.custom(weight == .bold ? DisplayFace.extraBold : DisplayFace.bold, fixedSize: size * 0.58))
            .monospacedDigit()
            .foregroundStyle(colour)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .frame(width: size, height: size)
            .background(Circle().fill(onStage ? Palette.stageRaised : Palette.surface))
            .overlay(Circle().strokeBorder(colour, lineWidth: Line.drawing))
            .accessibilityLabel("\(number)")
    }

    /// The stage marker as an image for SceneKit, RealityKit and AR (rendered once per number, tone and scale).
    @MainActor
    static func image(number: Int, tone: Tone, size: CGFloat = 26, scale: CGFloat = 3) -> UIImage {
        let key = "\(number)|\(tone)|\(size)|\(scale)"
        if let hit = imageCache[key] { return hit }
        let renderer = ImageRenderer(content: Marker(number: number, tone: tone, size: size, onStage: true)
            .environment(\.colorScheme, .dark))
        renderer.scale = scale
        renderer.isOpaque = false
        let image = renderer.uiImage ?? UIImage()
        imageCache[key] = image
        return image
    }

    @MainActor private static var imageCache: [String: UIImage] = [:]
}

// MARK: - Lea's mark

/// Lea's mark: a ring with 12 ticks inside the rim and a centre point (the astrolabe), always in the blue accent.
/// The ticks are her voice level: yours while she listens, hers while she speaks; while she thinks, three lit ticks
/// walk round (only while a real request runs). Idle ticks are at 30%. Without a real level, listening and speaking
/// show a static waveform state (never fake movement); Reduce Motion shows static glyphs. Pair it with words.
struct LeaMarkView: View {
    enum Phase: Equatable, Sendable {
        case idle, listening, speaking, thinking

        var words: String {
            switch self {
            case .idle: "Lea"
            case .listening: "Lea, listening"
            case .speaking: "Lea, speaking"
            case .thinking: "Lea, thinking"
            }
        }
    }

    var state: Phase = .idle
    /// 16, 24, 38 or 88.
    var size: CGFloat = 38
    /// The real voice level, 0-1, at most ten times a second, while listening or speaking; nil when none is plumbed.
    var level: Double?
    /// The ring colour; `Palette.next` over the camera or the stage.
    var tint: Color = Palette.accent
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let walking = state == .thinking && !reduceMotion
        TimelineView(.animation(minimumInterval: 0.12, paused: !walking)) { context in
            Canvas { ctx, canvas in
                draw(&ctx, canvas, step: walking ? Int(context.date.timeIntervalSinceReferenceDate / 0.12) % 12 : 0)
            }
        }
        .frame(width: size, height: size)
        .accessibilityElement()
        .accessibilityLabel(state.words)
    }

    private func draw(_ ctx: inout GraphicsContext, _ canvas: CGSize, step: Int) {
        let s = min(canvas.width, canvas.height)
        let c = CGPoint(x: canvas.width / 2, y: canvas.height / 2)
        let line = max(1, s / 38 * Line.drawing)
        let r = s / 2 - line / 2
        ctx.stroke(Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r)), with: .color(tint),
                   lineWidth: line)
        let active = state == .listening || state == .speaking
        let level = active ? self.level.map { min(1, max(0, $0)) } : nil
        for i in 0..<12 {
            let a = Double(i) / 12 * 2 * .pi - .pi / 2
            var opacity = 0.3
            var length = s * 0.14
            switch state {
            case .idle: break
            case .thinking:
                let lit = (0..<3).map { (step + $0) % 12 }
                opacity = lit.contains(i) ? 1 : 0.3
            case .listening, .speaking:
                if let level {
                    // Ticks lengthen with the real level; a small per-tick spread so it reads as a voice, not a ring.
                    let spread = 0.55 + 0.45 * abs(sin(Double(i) * 1.7))
                    length = s * (0.08 + 0.16 * level * spread)
                    opacity = 0.45 + 0.55 * level
                } else {
                    opacity = 1
                }
            }
            let outer = r - line * 1.4
            let inner = outer - length
            var p = Path()
            p.move(to: CGPoint(x: c.x + outer * cos(a), y: c.y + outer * sin(a)))
            p.addLine(to: CGPoint(x: c.x + inner * cos(a), y: c.y + inner * sin(a)))
            ctx.stroke(p, with: .color(tint.opacity(opacity)), style: StrokeStyle(lineWidth: max(1, line * 0.8), lineCap: .butt))
        }
        if active && level == nil && s >= 38 {
            // No level plumbed: a static waveform glyph says "listening/speaking" without pretending to move.
            var glyph = ctx.resolve(Image(systemName: "waveform").renderingMode(.template))
            glyph.shading = .color(tint)
            let gs = s * 0.32
            ctx.draw(glyph, in: CGRect(x: c.x - gs / 2, y: c.y - gs / 2, width: gs, height: gs))
        } else {
            let d = max(2, s * 0.1)
            ctx.fill(Path(ellipseIn: CGRect(x: c.x - d / 2, y: c.y - d / 2, width: d, height: d)), with: .color(tint))
        }
    }
}

// MARK: - Captions

/// One caption: the speaker's name (`label`; Lea's in the accent) over the words in `ink` (or chalk on the stage).
/// Lea speaks in captions, never chat bubbles. `live` (still being said or heard) is at 70%.
struct CaptionLine: View {
    enum Speaker: Sendable { case you, lea }
    let speaker: Speaker
    let text: String
    var live = false
    var onStage = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(speaker == .lea ? "Lea" : "You")
                .font(.label)
                .foregroundStyle(speaker == .lea ? (onStage ? Palette.next : Palette.accent) : (onStage ? Palette.chalk.opacity(0.7) : Palette.secondary))
            Text(text)
                .font(.body)
                .foregroundStyle(onStage ? Palette.chalk : Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
        .opacity(live ? 0.7 : 1)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Progress

/// The report's progress orb as a construction drawing: a hairline circle, an inscribed square and cross lines, with
/// the progress arc (2.5 pt, ink) following the real fraction. Failed: the arc and a warning glyph in danger.
/// A nil fraction draws no arc (no invented progress).
struct ConstructionOrb: View {
    var fraction: Double?
    var failed = false
    var size: CGFloat = 132

    var body: some View {
        ZStack {
            Canvas { ctx, canvas in
                let s = min(canvas.width, canvas.height)
                let c = CGPoint(x: canvas.width / 2, y: canvas.height / 2)
                let r = s / 2 - Line.locked
                let rule = GraphicsContext.Shading.color(Palette.rule.opacity(0.45))
                ctx.stroke(Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r)), with: rule, lineWidth: Line.hairline)
                let h = r / 2.squareRoot()
                ctx.stroke(Path(CGRect(x: c.x - h, y: c.y - h, width: 2 * h, height: 2 * h)), with: rule, lineWidth: Line.hairline)
                var cross = Path()
                cross.move(to: CGPoint(x: c.x - r, y: c.y)); cross.addLine(to: CGPoint(x: c.x + r, y: c.y))
                cross.move(to: CGPoint(x: c.x, y: c.y - r)); cross.addLine(to: CGPoint(x: c.x, y: c.y + r))
                cross.move(to: CGPoint(x: c.x - h, y: c.y - h)); cross.addLine(to: CGPoint(x: c.x + h, y: c.y + h))
                ctx.stroke(cross, with: rule, lineWidth: Line.hairline)
            }
            if let fraction {
                Circle()
                    .trim(from: 0, to: max(0.01, min(1, fraction)))
                    .stroke(failed ? Palette.danger : Palette.ink, style: StrokeStyle(lineWidth: Line.locked, lineCap: .butt))
                    .rotationEffect(.degrees(-90))
                    .padding(Line.locked)
                    .motion(Motion.standard, value: fraction)
            }
            if failed {
                Image(systemName: Symbol.safety).font(.system(size: size * 0.2)).foregroundStyle(Palette.danger)
            }
        }
        .frame(width: size, height: size)
        .accessibilityElement()
        .accessibilityLabel(failed ? "Failed" : "Progress")
        .accessibilityValue(fraction.map { "\(Int(($0 * 100).rounded())) percent" } ?? "")
    }
}

// MARK: - Previews

#Preview("Components") {
    ScrollView {
        VStack(alignment: .leading, spacing: Spacing.l) {
            SectionHeader("Most likely problems", count: 3)
            HStack(spacing: Spacing.m) {
                ToneTag(text: "Safety risk", tone: .danger, symbol: Symbol.safety)
                ToneTag(text: "Functional issue", tone: .caution)
                ToneTag(text: "Not inspected", tone: .neutral, symbol: Symbol.notInspected, dashed: true)
            }
            HStack(spacing: Spacing.m) {
                Marker(number: 1, tone: .danger); Marker(number: 2, tone: .caution); Marker(number: 3, tone: .cosmetic)
                LeaMarkView(); LeaMarkView(state: .thinking); LeaMarkView(state: .listening); LeaMarkView(state: .speaking, level: 0.7)
            }
            RuledRows(rows: [.init("Where", "Back, lower left"), .init("Confidence", "Medium")])
            GroupedList {
                Text("First row")
                Text("Second row")
            }
            Text("A card").card()
            Text("A safety card").card(tone: .danger)
            ConstructionOrb(fraction: 0.4)
            Button { } label: { Label("Start scan", systemImage: Symbol.scan) }.buttonStyle(.primary)
            Button { } label: { Text("Talk to Lea") }.buttonStyle(.primary(.quiet))
        }
        .padding(Spacing.gutter)
    }
    .background(Palette.canvas)
}
