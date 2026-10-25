import QuartzCore
import SwiftUI
import UIKit

// lea's design tokens (DESIGN.md §3-§6, UXHarness/palette.json): violet actions on lavender by day and deep violet by
// night, four status tones that carry meaning plus the calm setup tone, and camera tokens that stay the same in both
// modes. Display type is Alegreya Sans (SIL OFL 1.1, bundled); reading type is SF Pro at Dynamic Type sizes. Every
// value here is a choice the screens share; screens never invent their own.

// MARK: - Colour

extension Color {
    /// A colour that follows light and dark mode. `light`/`dark` are 0xRRGGBB; alpha applies to both.
    init(light: UInt32, dark: UInt32, alpha: Double = 1) {
        self.init(uiColor: UIColor(light: light, dark: dark, alpha: alpha))
    }

    /// A colour with its own alpha in each mode (the hairline, the glass fill and edge).
    init(light: UInt32, lightAlpha: Double, dark: UInt32, darkAlpha: Double) {
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(hex: dark, alpha: darkAlpha) : UIColor(hex: light, alpha: lightAlpha)
        })
    }

    /// The same colour in both modes (camera and stage tokens).
    init(fixed hex: UInt32, alpha: Double = 1) {
        self.init(uiColor: UIColor(hex: hex, alpha: alpha))
    }
}

extension UIColor {
    convenience init(light: UInt32, dark: UInt32, alpha: Double = 1) {
        self.init { traits in
            let hex = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(hex: hex, alpha: alpha)
        }
    }

    convenience init(hex: UInt32, alpha: Double = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }
}

enum Palette {
    // Lavender (light) and Violet night (dark).

    /// Screen background.
    static let canvas = Color(light: 0xF3F1FA, dark: 0x12101B)
    /// Opaque reading and safety surfaces, grouped rows.
    static let surface = Color(light: 0xFBFAFF, dark: 0x211D30)
    /// Inputs and thumbnail placeholders.
    static let well = Color(light: 0xE9E4F5, dark: 0x2C263D)
    /// 0.5 pt rules and subtle edges.
    static let hairline = Color(light: 0x746293, lightAlpha: 0.18, dark: 0xC7B5F1, darkAlpha: 0.22)
    /// Primary text.
    static let ink = Color(light: 0x211C32, dark: 0xF5F1FF)
    /// Secondary text and neutral labels.
    static let secondary = Color(light: 0x625B73, dark: 0xBEB5D0)
    /// Disabled content only; never essential text.
    static let faint = Color(light: 0x8B9399, dark: 0x6E767D)
    /// The one action colour: primary actions, links, selection and Lea.
    static let accent = Color(light: 0x6650CC, dark: 0xC2B1FF)
    /// A label on an accent fill (dark on the lavender dark-mode accent: never a hard-coded white).
    static let onAccent = Color(light: 0xFBFAFF, dark: 0x21143B)
    /// Construction lines (the dome on Home, the orb, the stage ground in the app).
    static let rule = Color(light: 0x211C32, dark: 0xBFB2D8)
    /// Frosted glass: fill and restrained edge (DESIGN §3, `Material/Frosted glass`).
    static let glassFill = Color(light: 0xFFFFFF, lightAlpha: 0.76, dark: 0x302844, darkAlpha: 0.82)
    static let glassEdge = Color(light: 0xFFFFFF, lightAlpha: 0.92, dark: 0xDDD1FF, darkAlpha: 0.24)

    // Status tones.

    /// No visible issues in an inspected area; covered dome.
    static let good = Color(light: 0x3A7536, dark: 0xB5D58A)
    /// Functional issue or capture caution (passes 4.5:1 as text in both modes).
    static let caution = Color(light: 0x8A5F00, dark: 0xE9B24F)
    /// Safety risk, blocking cue or failure.
    static let danger = Color(light: 0x8E2316, dark: 0xF0785C)
    /// Cosmetic finding.
    static let cosmetic = Color(light: 0x75638C, dark: 0xC2B3DB)
    /// Setup to check (a battery missing, a cable unplugged): calm teal, apart from the fault tones. Not broken.
    /// 4.54:1 on the light canvas, 9.72:1 on the dark one.
    static let setup = Color(light: 0x0B7A84, dark: 0x5CCAD3)

    // Camera and stage (the same in both modes: the camera is never "light").

    /// Lines and labels over the camera and the stage.
    static let chalk = Color(fixed: 0xF4F2EC)
    /// The stroke 2 pt wider under chalk, so it survives a bright scene.
    static let keyline = Color(fixed: 0x0E1214, alpha: 0.8)
    /// Cue, chips, the dial and round controls over the camera or the stage.
    static let smoke = Color(fixed: 0x0E1214, alpha: 0.72)
    /// `smoke` under Reduce Transparency.
    static let smokeSolid = Color(fixed: 0x0E1214)
    /// The next coverage cell, locked brackets, Lea's circles and selection over the camera.
    static let next = Color(fixed: 0xC2B1FF)
    /// The flat scan stage in the report, in both appearances.
    static let stage = Color(fixed: 0x12101B)
    /// Status tones over the camera: the dark-mode values, which read on smoke whatever the phone's mode.
    static let cameraGood = Color(fixed: 0xB5D58A)
    static let cameraCaution = Color(fixed: 0xE9B24F)
    static let cameraDanger = Color(fixed: 0xF0785C)
    /// The dark glyph inside a camera tone disc.
    static let cameraGlyph = Color(fixed: 0x0E1214)
}

/// What a colour means. Severity, cue urgency and status all map onto these, never onto raw colours. `.accent` is
/// a tone only for selection and Lea; it is never a status.
enum Tone: Sendable, Hashable {
    case accent, good, caution, danger, cosmetic, setup, neutral

    var color: Color {
        switch self {
        case .accent: Palette.accent
        case .good: Palette.good
        case .caution: Palette.caution
        case .danger: Palette.danger
        case .cosmetic: Palette.cosmetic
        case .setup: Palette.setup
        case .neutral: Palette.secondary
        }
    }

    /// The colour for words in this tone (every tone passes 4.5:1 as text now).
    var text: Color { color }

    /// The fill behind the safety card (10% light, 16% dark). Tags are words and icons, never a wash.
    var wash: Color {
        let base = UIColor(color)
        return Color(uiColor: UIColor { traits in
            base.resolvedColor(with: traits).withAlphaComponent(traits.userInterfaceStyle == .dark ? 0.16 : 0.10)
        })
    }

    /// The same tone over the camera or the stage (the dark-mode value, fixed).
    var cameraColor: Color {
        Color(uiColor: UIColor(color).resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark)))
    }
}

// MARK: - Layout

enum Spacing {
    static let xxs: CGFloat = 2
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
    static let xxl: CGFloat = 32
    /// Side margin of every scrolling screen.
    static let gutter: CGFloat = 20
}

/// Labels pinned on a board, not bubbles.
enum Radius {
    static let small: CGFloat = 6
    static let medium: CGFloat = 10
    static let card: CGFloat = 12
    static let thumb: CGFloat = 6
    /// The report sheet's top corners.
    static let sheet: CGFloat = 16
}

/// Line weights (DESIGN §5). Ends are square and corners mitred; the dash is for unknown only.
enum Line {
    static let hairline: CGFloat = 0.5
    /// Construction lines and empty dome cells.
    static let construction: CGFloat = 1
    /// Searching brackets, drawings and Lea's mark.
    static let drawing: CGFloat = 1.5
    /// Locked brackets and the progress arc.
    static let locked: CGFloat = 2.5
    static let dash: [CGFloat] = [3, 3]
}

// MARK: - Type

/// The bundled display face (PostScript names; registered through `UIAppFonts`).
enum DisplayFace {
    static let bold = "AlegreyaSans-Bold"
    static let extraBold = "AlegreyaSans-ExtraBold"
    static let black = "AlegreyaSans-Black"
    static let all = [bold, extraBold, black]
}

/// Display type is Alegreya Sans, scaled with Dynamic Type; reading, labels and buttons are SF Pro.
extension Font {
    static let heroTitle = Font.custom(DisplayFace.bold, size: 34, relativeTo: .largeTitle)
    static let screenTitle = Font.custom(DisplayFace.bold, size: 28, relativeTo: .title)
    static let sectionTitle = Font.custom(DisplayFace.bold, size: 22, relativeTo: .title3)
    static let cardTitle = Font.custom(DisplayFace.bold, size: 19, relativeTo: .headline)
    /// Problem and step numbers.
    static let number = Font.custom(DisplayFace.bold, size: 15, relativeTo: .subheadline)
    /// The wordmark is a logo, so it does not scale with Dynamic Type. Always lowercase `lea`, tracking -1 %.
    static let wordmark = Font.custom(DisplayFace.extraBold, fixedSize: 34)
    static let buttonLabel = Font.system(.headline, weight: .semibold)
    static let pillLabel = Font.system(.caption, weight: .semibold)
    /// Field names, tags, dates; sentence case.
    static let label = Font.system(.footnote, weight: .semibold)
    /// Confidence and likelihood words.
    static let judgement = Font.system(.subheadline)
    /// The capture cue: short, heavy enough to read over a busy camera picture.
    static let cue = Font.system(.callout, weight: .semibold)
    static let metric = Font.system(.title2, weight: .semibold).monospacedDigit()
}

/// The display styles, for `View.display(_:)`, which steps the weight up under Bold Text.
enum DisplayStyle: Sendable {
    case hero, screen, section, card, number, wordmark

    func font(bold: Bool) -> Font {
        switch self {
        case .hero: .custom(bold ? DisplayFace.extraBold : DisplayFace.bold, size: 34, relativeTo: .largeTitle)
        case .screen: .custom(bold ? DisplayFace.extraBold : DisplayFace.bold, size: 28, relativeTo: .title)
        case .section: .custom(bold ? DisplayFace.extraBold : DisplayFace.bold, size: 22, relativeTo: .title3)
        case .card: .custom(bold ? DisplayFace.extraBold : DisplayFace.bold, size: 19, relativeTo: .headline)
        case .number: .custom(bold ? DisplayFace.extraBold : DisplayFace.bold, size: 15, relativeTo: .subheadline)
        case .wordmark: .custom(bold ? DisplayFace.black : DisplayFace.extraBold, fixedSize: 34)
        }
    }
}

private struct DisplayModifier: ViewModifier {
    let style: DisplayStyle
    @Environment(\.legibilityWeight) private var weight
    func body(content: Content) -> some View {
        content.font(style.font(bold: weight == .bold))
    }
}

extension View {
    /// A display face that follows Bold Text (Bold → ExtraBold, ExtraBold → Black). Use it on titles; the static
    /// `Font.heroTitle` and friends stay for `Text` concatenation.
    func display(_ style: DisplayStyle) -> some View { modifier(DisplayModifier(style: style)) }
}

// MARK: - Motion

/// Springs follow Apple's defaults: critically damped for UI, a little bounce only for a physical moment (the
/// lock settling, a shot landing). Under Reduce Motion every movement becomes a short fade.
enum Motion {
    /// Button press-down and release.
    static let press = Animation.spring(response: 0.22, dampingFraction: 1)
    /// Most state changes: cards expanding, pills changing, the cue changing.
    static let standard = Animation.spring(response: 0.35, dampingFraction: 1)
    /// The lock box settling, the shot tick landing.
    static let settle = Animation.spring(response: 0.4, dampingFraction: 0.78)
    /// Capture → report: a slower, calm fade after the Done moment. Entering, so ease-out (it starts moving at once).
    static let reveal = Animation.easeOut(duration: 0.45)
    /// Something arriving with a little life: a marker or a finding popping in (from 0.6 scale, never from 0).
    static let pop = Animation.spring(response: 0.35, dampingFraction: 0.8)
    /// A surface rising into place (the report sheet): critically damped, no overshoot.
    static let rise = Animation.spring(response: 0.4, dampingFraction: 1)
    /// The walkthrough gliding to its next card, the camera with it: slow, critically damped, no overshoot.
    static let glide = Animation.spring(response: 0.95, dampingFraction: 1)
    /// Gap between items that arrive together (markers, rows). Only the first few are staggered.
    static let stagger: Double = 0.06
    /// Colour and opacity only; also the Reduce Motion replacement.
    static let fade = Animation.easeOut(duration: 0.2)
    /// A caption or status swapping its words (120 ms opacity).
    static let caption = Animation.easeOut(duration: 0.12)

    static func pick(_ animation: Animation, reduceMotion: Bool) -> Animation {
        reduceMotion ? fade : animation
    }

    /// SceneKit timings for Lea moving the 3D model (DESIGN §6, the redesign plan §3.2). One curve for every move:
    /// the one `RecreationScene.move` already uses.
    enum Scene {
        /// Fly to a part (`show`, a resolved `point`, a chosen marker); scale 0.6-1.0 s by angular distance.
        static let fly: Double = 0.8
        static let flyRange: ClosedRange<Double> = 0.6...1.0
        /// Orbit to a side (`view top/bottom/front/back/left/right`).
        static let orbit: Double = 0.9
        /// One full turn (`view around`), never looping.
        static let around: Double = 2.4
        /// Dolly in or out (`zoom`).
        static let zoom: Double = 0.5
        /// The Reduce Motion crossfade that replaces every move.
        static let cut: Double = 0.2
        /// The highlight (ring draw-on, surface light, label) starts when the camera is this far settled.
        static let ringStart: Double = 0.8
        /// The ring's draw-on and the surface highlight's radius growing in.
        static let drawOn: Double = 0.35
        /// Quiet time before the gentle idle drift resumes (nobody touching, Lea silent).
        static let idleDriftAfter: Double = 10
        /// Zoom factors (dolly ×0.6 in, ×1.6 out).
        static let zoomIn: Float = 0.6
        static let zoomOut: Float = 1.6

        static var curve: CAMediaTimingFunction { CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0, 1) }
    }
}

private struct MotionModifier<V: Equatable>: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let animation: Animation
    let value: V
    func body(content: Content) -> some View {
        content.animation(Motion.pick(animation, reduceMotion: reduceMotion), value: value)
    }
}

extension View {
    /// `.animation(_:value:)` that turns into a fade under Reduce Motion.
    func motion<V: Equatable>(_ animation: Animation = Motion.standard, value: V) -> some View {
        modifier(MotionModifier(animation: animation, value: value))
    }
}

// MARK: - Haptics

/// One haptic per meaningful moment, fired on the same frame as its visual. Nothing else buzzes.
enum Haptic {
    /// The object is locked.
    static let lock = SensoryFeedback.impact(weight: .medium, intensity: 0.9)
    /// A photo was accepted (the shot tick).
    static let shot = SensoryFeedback.impact(flexibility: .rigid, intensity: 0.55)
    /// A coverage row or the whole dome completed; the scan has enough.
    static let milestone = SensoryFeedback.success
    /// A cue turned to caution or stop.
    static let warn = SensoryFeedback.warning
    /// Primary button commit.
    static let commit = SensoryFeedback.impact(weight: .light, intensity: 0.7)
    /// The report landed with likely problems (same frame as the verdict rising and Lea starting to speak).
    static let verdictProblems = SensoryFeedback.warning
    /// The report landed and nothing looked wrong.
    static let verdictClear = SensoryFeedback.success
    /// A marker or a problem row chosen (the camera flies to it).
    static let select = SensoryFeedback.selection
    /// Lea's move on the 3D model landed (once per move, never per frame).
    static let leaShow = SensoryFeedback.selection
}

// MARK: - Symbols

/// SF Symbols used in more than one place, so a concept always has the same picture. Outline variants, so they sit
/// with the hairlines.
enum Symbol {
    static let scan = "viewfinder"
    static let lock = "scope"
    static let mac = "server.rack"
    static let pair = "laptopcomputer.and.iphone"
    static let settings = "gearshape"
    static let close = "xmark"
    static let safety = "exclamationmark.triangle"
    static let good = "checkmark.seal"
    static let unknown = "questionmark.circle"
    static let closeup = "camera.macro"
    static let photo = "photo"
    static let privacy = "hand.raised"
    static let notInspected = "eye.slash"
    static let couldNotJudge = "questionmark.square.dashed"
    static let cosmetic = "sparkles"
    static let nextStep = "arrow.forward.circle"
    static let setup = "wrench.adjustable"
    /// Lea, where her drawn mark (`LeaMarkView`) can't go.
    static let lea = "circle.dotted"
}
