import SwiftUI
import UIKit

// lea's design tokens. One accent (iris), four tones that carry meaning (good, caution, danger, cosmetic), a
// near-black dark mode and a soft grey light mode. System fonts only: SF Rounded for display and numbers, SF Pro
// for reading. Every value here is a choice the screens share; screens never invent their own.

// MARK: - Colour

extension Color {
    /// A colour that follows light and dark mode. `light`/`dark` are 0xRRGGBB; alpha applies to both.
    init(light: UInt32, dark: UInt32, alpha: Double = 1) {
        self.init(uiColor: UIColor(light: light, dark: dark, alpha: alpha))
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
    /// The brand accent: buttons, the next coverage cell, selection.
    static let accent = Color(light: 0x5B4DF5, dark: 0x8C83FF)
    /// Fill behind white labels: a touch deeper than `accent` in dark mode so white text keeps 4.5:1.
    static let accentFill = Color(light: 0x5B4DF5, dark: 0x6A5EF2)
    /// Accent for marks drawn over the live camera (same in both modes: the camera is never "light").
    static let cameraAccent = Color(uiColor: UIColor(hex: 0x9C94FF))

    /// Screen background.
    static let canvas = Color(light: 0xF2F2F6, dark: 0x050507)
    /// Cards and grouped rows.
    static let surface = Color(light: 0xFFFFFF, dark: 0x141418)
    /// Wells inside a card (inputs, thumbnails while loading).
    static let well = Color(light: 0xF0F0F4, dark: 0x202026)
    /// 0.5 pt borders that keep cards crisp without shadows.
    static let hairline = Color(light: 0x000000, dark: 0xFFFFFF, alpha: 0.08)

    static let good = Color(light: 0x1F9D55, dark: 0x3DDC84)
    static let caution = Color(light: 0xC76B00, dark: 0xFFB340)
    static let danger = Color(light: 0xD92D3A, dark: 0xFF5C66)
    static let cosmetic = Color(light: 0x5E6B80, dark: 0xA4B0C4)
    /// Setup to check (a battery missing, a cable unplugged): calm teal, apart from the fault tones. Not broken.
    static let setup = Color(light: 0x0B7A84, dark: 0x5CCAD3)
    /// Caution as text: `caution` is 3.8:1 on white, too light for small words; this is ≈5.2:1 (dark mode unchanged).
    static let cautionText = Color(light: 0xA35A00, dark: 0xFFB340)

    /// The report's dark stage (the scan in 3D), the same in light and dark mode, like the camera and the big
    /// screen: a radial gradient from `stageCenter` to `stageEdge`, with a faint accent floor.
    static let stageCenter = Color(uiColor: UIColor(hex: 0x14121F))
    static let stageEdge = Color(uiColor: UIColor(hex: 0x050508))
    /// A glass control on the stage (white at 12%), and its solid stand-in under Reduce Transparency.
    static let stageGlass = Color.white.opacity(0.12)
    static let stageGlassSolid = Color(uiColor: UIColor(hex: 0x23232B))
}

/// What a colour means. Severity, cue urgency and status all map onto these, never onto raw colours.
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
        case .neutral: Color.secondary
        }
    }

    /// The colour for words in this tone (caution text is darker than the caution fill in light mode).
    var text: Color { self == .caution ? Palette.cautionText : color }

    /// Fill behind a tinted label or icon.
    var wash: Color { color.opacity(self == .neutral ? 0.12 : 0.14) }
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

enum Radius {
    static let small: CGFloat = 8
    static let medium: CGFloat = 14
    static let card: CGFloat = 22
    static let thumb: CGFloat = 12
}

// MARK: - Type

/// Dynamic Type text styles throughout; display sizes use SF Rounded, reading text SF Pro.
extension Font {
    static let heroTitle = Font.system(.largeTitle, design: .rounded, weight: .bold)
    static let screenTitle = Font.system(.title, design: .rounded, weight: .bold)
    static let sectionTitle = Font.system(.title3, design: .rounded, weight: .semibold)
    static let cardTitle = Font.system(.headline)
    static let buttonLabel = Font.system(.headline, design: .rounded, weight: .semibold)
    static let pillLabel = Font.system(.footnote, design: .rounded, weight: .semibold)
    /// The capture cue: short, heavy enough to read over a busy camera picture.
    static let cue = Font.system(.callout, design: .rounded, weight: .semibold)
    static let metric = Font.system(.title2, design: .rounded, weight: .semibold).monospacedDigit()
    /// The wordmark is a logo, so it does not scale with Dynamic Type.
    static let wordmark = Font.system(size: 30, weight: .bold, design: .rounded)
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

    static func pick(_ animation: Animation, reduceMotion: Bool) -> Animation {
        reduceMotion ? fade : animation
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
    /// A coverage row or the whole dome completed.
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
}

// MARK: - Symbols

/// SF Symbols used in more than one place, so a concept always has the same picture.
enum Symbol {
    static let scan = "viewfinder"
    static let lock = "scope"
    static let mac = "server.rack"
    static let pair = "laptopcomputer.and.iphone"
    static let settings = "gearshape.fill"
    static let close = "xmark"
    static let safety = "exclamationmark.triangle.fill"
    static let good = "checkmark.seal.fill"
    static let unknown = "questionmark.circle.fill"
    static let closeup = "camera.macro"
    static let photo = "photo"
    static let privacy = "hand.raised.fill"
    static let notInspected = "eye.slash"
    static let couldNotJudge = "questionmark.square.dashed"
    static let cosmetic = "sparkles"
    static let nextStep = "arrow.forward.circle.fill"
}
