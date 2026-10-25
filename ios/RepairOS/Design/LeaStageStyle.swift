import SwiftUI
import UIKit

// The look of Lea driving the 3D model and pointing at evidence (redesign plan §3.2): the highlight ring style, the
// part label, the caption strip on the stage, and a finding's photo with its box marked. Styling only: the move model
// and camera maths live in Report/Stage/LeaStage.swift (StageCommand, LeaStagePlan, StageCamera, StageTiming), and
// this file never duplicates them. Timings are `Motion.Scene` (Theme). Everything here sits on the stage or on
// evidence, so it is chalk and smoke: no glass, no blur.

// MARK: - Highlight ring

/// How Lea's highlight ring looks: a finding's ring in its severity tone, any other part or point in `next`. Chalk
/// keyline under it so it reads on any scan. It settles once (scale 0.85 → 1, stroke 1.5 → 2.5 pt) and never pulses.
struct FocusRingStyle: Equatable, Sendable {
    /// The finding's tone; nil for a part or point that is not a finding. Always a solid ring: the stage never draws
    /// a dashed "look closer" ring (the owner, 2026-10-25; unchecked areas live behind ⋯ "What wasn't checked").
    var tone: Tone?

    static let startScale: CGFloat = 0.85
    static let startWidth: CGFloat = Line.drawing
    static let endWidth: CGFloat = Line.locked
    /// Other markers and rings dim to this while Lea points at one.
    static let dimmed: Double = 0.4

    var color: Color { tone?.cameraColor ?? Palette.next }

    func stroke(width: CGFloat) -> StrokeStyle {
        StrokeStyle(lineWidth: width, lineCap: .butt)
    }

    /// The settled ring as an image for a SceneKit or RealityKit plane (rendered once per style and size).
    @MainActor
    func image(diameter: CGFloat = 128, scale: CGFloat = 2) -> UIImage {
        let key = "\(String(describing: tone))|\(diameter)|\(scale)"
        if let hit = Self.cache[key] { return hit }
        let view = Circle()
            .stroke(Palette.keyline, style: stroke(width: Self.endWidth + 2))
            .overlay(Circle().stroke(color, style: stroke(width: Self.endWidth)))
            .padding(Self.endWidth + 2)
            .frame(width: diameter, height: diameter)
        let renderer = ImageRenderer(content: view)
        renderer.scale = scale
        renderer.isOpaque = false
        let image = renderer.uiImage ?? UIImage()
        Self.cache[key] = image
        return image
    }

    @MainActor private static var cache: [String: UIImage] = [:]
}

/// The highlight ring as a SwiftUI view (over a photo hero, or projected over the 3D view). Trigger: `shown` turns
/// true (Lea's move reached 60% of its flight). End: it holds until `shown` turns false. Reduce Motion: it appears at
/// its final size with a 120 ms fade.
struct FocusRing: View {
    var style = FocusRingStyle()
    var shown = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let settled = shown || reduceMotion
        let width = settled ? FocusRingStyle.endWidth : FocusRingStyle.startWidth
        ZStack {
            Circle().stroke(Palette.keyline, style: style.stroke(width: width + 2))
            Circle().stroke(style.color, style: style.stroke(width: width))
        }
        .scaleEffect(settled ? 1 : FocusRingStyle.startScale)
        .opacity(shown ? 1 : 0)
        .animation(reduceMotion ? Motion.caption : Motion.pop, value: shown)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - Part label

/// The name of the part Lea is pointing at ("Power button", or a `point` label like "A2"): chalk on smoke, with an
/// optional 1 pt chalk leader down-left toward the ring. It fades in with the ring (`Motion.caption`).
struct PartLabel: View {
    let text: String
    var marker: (number: Int, tone: Tone)?
    var leader = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                if let marker { Marker(number: marker.number, tone: marker.tone, size: 18, onStage: true) }
                Text(text).font(.label).foregroundStyle(Palette.chalk).lineLimit(1)
            }
            .padding(.leading, marker == nil ? 10 : 4).padding(.trailing, 10).padding(.vertical, 5)
            .background { SmokeFill(shape: Capsule()) }
            if leader {
                LeaderLine().stroke(Palette.chalk, lineWidth: 1)
                    .background(LeaderLine().stroke(Palette.keyline, lineWidth: 3))
                    .frame(width: 18, height: 18)
                    .padding(.leading, 6)
            }
        }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .accessibilityElement(children: .combine)
    }

    private struct LeaderLine: Shape {
        func path(in rect: CGRect) -> Path {
            var p = Path()
            p.move(to: CGPoint(x: rect.maxX * 0.5, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
            return p
        }
    }

    /// The label as an image, for a SceneKit billboard when the stage can't project a point to the screen.
    @MainActor
    static func image(_ text: String, scale: CGFloat = 3) -> UIImage {
        let renderer = ImageRenderer(content: PartLabel(text: text).environment(\.colorScheme, .dark))
        renderer.scale = scale
        renderer.isOpaque = false
        return renderer.uiImage ?? UIImage()
    }
}

// MARK: - Caption strip

/// Lea's words on the stage, synced to her voice: your last question on one line (chalk at 70%), then the sentence
/// she is saying now (chalk, up to three lines). A sentence that carried a `show` is prefixed by the part's marker or
/// a `next` dot and its name. Thinking shows her mark walking (only while a real request runs); a failure shows one
/// caution line. A new sentence replaces the old with a 120 ms crossfade (`Motion.caption`), never a typewriter. The
/// owner of the strip decides when it shows (hidden until the first turn and during the walkthrough) and what a tap
/// does (the transcript).
struct LeaCaptionStrip: View {
    struct Part: Equatable, Sendable {
        var name: String
        var number: Int?
        var tone: Tone?
    }

    var question: String?
    var sentence: String?
    var part: Part?
    var thinking = false
    var failure: String?
    var tap: (() -> Void)?

    var body: some View {
        Button { tap?() } label: { content }
            .buttonStyle(PressableCardStyle())
            .disabled(tap == nil)
            .accessibilityHint(tap == nil ? "" : "Shows everything you and Lea said")
    }

    private var content: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            LeaMarkView(state: thinking ? .thinking : (sentence == nil ? .idle : .speaking), size: 24, tint: Palette.next)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                if let question = question?.trimmedOrNil {
                    // Two lines, so a real question is not cut to its first few words; the transcript has it whole.
                    Text(question).font(.subheadline).foregroundStyle(Palette.chalk.opacity(0.7)).lineLimit(2)
                        .truncationMode(.head)
                }
                if let failure = failure?.trimmedOrNil {
                    Label(failure, systemImage: Symbol.safety).font(.subheadline.weight(.semibold))
                        .foregroundStyle(Palette.cameraCaution)
                } else if thinking, sentence == nil {
                    Text("Thinking…").font(.body).foregroundStyle(Palette.chalk.opacity(0.7))
                } else if let sentence {
                    VStack(alignment: .leading, spacing: 4) {
                        if let part { partLine(part) }
                        // One sentence at a time, whole: the stage measures the strip and makes room for it.
                        Text(sentence).font(.body).foregroundStyle(Palette.chalk).lineLimit(6)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .id(sentence)
                    .transition(.opacity)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        .background { SmokeFill(shape: RoundedRectangle(cornerRadius: Radius.card, style: .continuous)) }
        .animation(Motion.caption, value: sentence)
        .animation(Motion.caption, value: thinking)
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityWords)
    }

    private func partLine(_ part: Part) -> some View {
        HStack(spacing: 6) {
            if let n = part.number, let tone = part.tone {
                Marker(number: n, tone: tone, size: 16, onStage: true)
            } else {
                Circle().fill(Palette.next).frame(width: 7, height: 7)
            }
            Text(part.name).font(.label).foregroundStyle(Palette.chalk)
        }
        .accessibilityElement(children: .combine)
    }

    private var accessibilityWords: String {
        var parts: [String] = []
        if let q = question?.trimmedOrNil { parts.append("You: \(q)") }
        if let f = failure?.trimmedOrNil { parts.append(f) }
        else if let s = sentence { parts.append("Lea: \(s)") }
        else if thinking { parts.append("Lea is thinking") }
        return parts.joined(separator: ". ")
    }
}

// MARK: - Evidence photo

/// A finding's full still with its `box_px` marked on it (the problem detail's photo-first layout, screen 18): the image
/// untouched (evidence keeps its colour), the box as a tone corner set (the lock-bracket shape) or Lea's ring, a 1 pt
/// leader from the box to the problem's `Marker`, and a chalk-on-smoke caption with the still's real label. No box:
/// the photo and caption only. (The crops of spots Lea shows in an answer arrive already ringed by the gateway and are
/// drawn by `SpotPhotos`; Stage B styles that view rather than replacing it.)
struct EvidencePhoto: View {
    enum Mark: Sendable { case corners, ring }

    let image: UIImage?
    /// The defect's box in the image, as fractions of its width and height (0-1).
    var box: CGRect?
    var tone: Tone = .caution
    var number: Int?
    /// The still's real label ("Photo 7"), never invented.
    var caption: String?
    var mark: Mark = .corners
    var height: CGFloat = 300

    var body: some View {
        ZStack {
            Palette.stage
            if let image {
                GeometryReader { geo in
                    let fit = fitted(image.size, in: geo.size)
                    Image(uiImage: image).resizable().interpolation(.high)
                        .frame(width: fit.width, height: fit.height)
                        .position(x: geo.size.width / 2, y: geo.size.height / 2)
                    if let box {
                        let r = CGRect(x: (geo.size.width - fit.width) / 2 + box.minX * fit.width,
                                       y: (geo.size.height - fit.height) / 2 + box.minY * fit.height,
                                       width: max(24, box.width * fit.width), height: max(24, box.height * fit.height))
                        marked(r, in: geo.size)
                    }
                }
            } else {
                Image(systemName: Symbol.photo).font(.title2).foregroundStyle(Palette.chalk.opacity(0.4))
            }
        }
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .overlay(alignment: .bottomLeading) {
            if let caption = caption?.trimmedOrNil {
                Text(caption).cameraChip().padding(Spacing.s)
            }
        }
        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(["Evidence photo", caption, number.map { "problem \($0) marked" }].compactMap { $0 }.joined(separator: ", "))
    }

    @ViewBuilder private func marked(_ r: CGRect, in size: CGSize) -> some View {
        let colour = tone.cameraColor
        switch mark {
        case .corners:
            Color.clear.frame(width: r.width, height: r.height)
                .keylined(CornerBrackets(), color: colour, width: Line.drawing)
                .position(x: r.midX, y: r.midY)
        case .ring:
            let d = max(r.width, r.height) * 1.25
            FocusRing(style: FocusRingStyle(tone: tone)).frame(width: d, height: d).position(x: r.midX, y: r.midY)
        }
        if let number {
            // The marker sits up and to the right of the box, inside the photo, joined by a 1 pt leader.
            let m = CGPoint(x: min(size.width - 18, r.maxX + 22), y: max(18, r.minY - 22))
            Path { p in
                p.move(to: CGPoint(x: r.maxX, y: r.minY))
                p.addLine(to: m)
            }
            .stroke(Palette.keyline, lineWidth: 3)
            Path { p in
                p.move(to: CGPoint(x: r.maxX, y: r.minY))
                p.addLine(to: m)
            }
            .stroke(colour, lineWidth: 1)
            Marker(number: number, tone: tone, onStage: true).position(m)
        }
    }

    private func fitted(_ image: CGSize, in view: CGSize) -> CGSize {
        guard image.width > 0, image.height > 0 else { return view }
        let s = min(view.width / image.width, view.height / image.height)
        return CGSize(width: image.width * s, height: image.height * s)
    }
}

#if DEBUG
#Preview("Lea on the stage") {
    ZStack {
        StageBackground().ignoresSafeArea()
        VStack(spacing: Spacing.l) {
            HStack(spacing: Spacing.xl) {
                FocusRing(style: FocusRingStyle(tone: .danger)).frame(width: 80, height: 80)
                FocusRing(style: FocusRingStyle()).frame(width: 80, height: 80)
                PartLabel(text: "Power button", leader: true)
            }
            Spacer()
            LeaCaptionStrip(question: "Where is the power button?", sentence: "It's the round one on the front, on the right.",
                            part: .init(name: "Power button"))
            LeaCaptionStrip(thinking: true)
        }
        .padding()
    }
}
#endif

// MARK: - Textures for SceneKit and RealityKit

/// The stage's marks as images for the 3D heroes (RecreationScene, ModelView, AR), drawn with CoreGraphics so they can
/// be made off the main actor: the `Marker` (a dark-surface disc outlined in the tone, the number in the display face)
/// and the `FocusRingStyle` ring (the tone over a keyline, solid; no glow). Colours are the
/// fixed stage values (the dark-mode tones; `next` for a part or point that is not a finding).
enum StageMarkImages {
    /// The stage colour for a tone, as sRGB.
    static func rgb(_ tone: Tone) -> SIMD3<Float> {
        let hex: UInt32 = switch tone {
        case .danger: FixedHex.danger
        case .caution: FixedHex.caution
        case .good: FixedHex.good
        case .cosmetic: FixedHex.cosmetic
        case .setup: FixedHex.setup
        case .accent, .neutral: FixedHex.next
        }
        return FixedHex.rgb(hex)
    }

    private static func context(_ side: Int) -> CGContext? {
        CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                  space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    }

    private static func colour(_ rgb: SIMD3<Float>) -> CGColor {
        CGColor(srgbRed: CGFloat(rgb.x), green: CGFloat(rgb.y), blue: CGFloat(rgb.z), alpha: 1)
    }

    /// The marker: `number` nil draws a small outlined dot (a point that is not a numbered problem).
    static func badge(number: Int?, rgb: SIMD3<Float>, side: Int = 96) -> CGImage? {
        guard let ctx = context(side) else { return nil }
        let s = CGFloat(side)
        let line = s * Line.drawing / 26
        let rect = CGRect(x: 0, y: 0, width: s, height: s).insetBy(dx: line / 2 + 1, dy: line / 2 + 1)
        ctx.setFillColor(FixedHex.cgColor(FixedHex.stageRaised))
        ctx.fillEllipse(in: rect)
        ctx.setStrokeColor(colour(rgb))
        ctx.setLineWidth(max(2, line))
        ctx.strokeEllipse(in: rect)
        if let number {
            let font = CTFontCreateWithName(DisplayFace.bold as CFString, s * 0.58, nil)
            let attrs: [NSAttributedString.Key: Any] = [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): colour(rgb)]
            let text = CTLineCreateWithAttributedString(NSAttributedString(string: "\(number)", attributes: attrs))
            let b = CTLineGetImageBounds(text, ctx)
            ctx.textPosition = CGPoint(x: (s - b.width) / 2 - b.minX, y: (s - b.height) / 2 - b.minY)
            CTLineDraw(text, ctx)
        } else {
            ctx.setFillColor(colour(rgb))
            let d = s * 0.32
            ctx.fillEllipse(in: CGRect(x: (s - d) / 2, y: (s - d) / 2, width: d, height: d))
        }
        return ctx.makeImage()
    }

    /// The highlight ring: the circle at 1/1.3 of the image (the heroes size their planes for that margin).
    static func ring(rgb: SIMD3<Float>, side: Int = 256) -> CGImage? {
        guard let ctx = context(side) else { return nil }
        let s = CGFloat(side), c = s / 2, r = s / 2 / 1.3
        let circle = CGRect(x: c - r, y: c - r, width: 2 * r, height: 2 * r)
        ctx.setStrokeColor(FixedHex.cgColor(FixedHex.keyline, alpha: 0.8))
        ctx.setLineWidth(s * 0.05)
        ctx.strokeEllipse(in: circle)
        ctx.setStrokeColor(colour(rgb))
        ctx.setLineWidth(s * 0.03)
        ctx.strokeEllipse(in: circle)
        return ctx.makeImage()
    }
}
