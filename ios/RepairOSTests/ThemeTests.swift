import SwiftUI
import Testing
import UIKit
@testable import RepairOS

/// The design tokens: the bundled display face is registered (a missing registration would fall back to the system
/// font silently), and the reading pairs keep 4.5:1 in both modes, computed from the tokens themselves.
struct ThemeTests {
    @Test func alegreyaSansIsRegistered() {
        for name in DisplayFace.all {
            #expect(UIFont(name: name, size: 17) != nil, "\(name) is not registered (UIAppFonts / Design/Fonts)")
        }
    }

    @Test(arguments: [UIUserInterfaceStyle.light, .dark])
    func onAccentKeepsContrast(_ style: UIUserInterfaceStyle) {
        #expect(Self.contrast(Palette.onAccent, Palette.accent, style) >= 4.5)
    }

    @Test(arguments: [UIUserInterfaceStyle.light, .dark])
    func onDangerKeepsContrast(_ style: UIUserInterfaceStyle) {
        #expect(Self.contrast(Palette.onDanger, Palette.danger, style) >= 4.5)
    }

    @Test(arguments: [UIUserInterfaceStyle.light, .dark])
    func readingTonesPassOnCanvasAndSurface(_ style: UIUserInterfaceStyle) {
        let tones: [Tone] = [.good, .caution, .danger, .cosmetic, .neutral, .setup, .accent]
        for background in [Palette.canvas, Palette.surface] {
            #expect(Self.contrast(Palette.ink, background, style) >= 4.5)
            #expect(Self.contrast(Palette.secondary, background, style) >= 4.5)
            for tone in tones {
                #expect(Self.contrast(tone.text, background, style) >= 4.5, "\(tone) in \(style == .dark ? "dark" : "light")")
            }
        }
    }

    @Test func chalkReadsOnSmokeOverBlackAndWhite() {
        // Smoke at 72% over the worst-case camera picture (pure white) still carries chalk text.
        let smokeOverWhite = Self.over(UIColor(hex: 0x0E1214, alpha: 0.72), UIColor.white)
        #expect(Self.ratio(Self.luminance(UIColor(hex: 0xF4F2EC)), Self.luminance(smokeOverWhite)) >= 4.5)
    }

    @Test(arguments: [UIUserInterfaceStyle.light, .dark])
    func readingPairsPassOnGlass(_ style: UIUserInterfaceStyle) {
        // Words on a glass card sit on the glass fill over the canvas.
        let traits = UITraitCollection(userInterfaceStyle: style)
        let glass = Self.over(UIColor(Palette.glassFill).resolvedColor(with: traits), UIColor(Palette.canvas).resolvedColor(with: traits))
        for (name, fg) in [("ink", Palette.ink), ("secondary", Palette.secondary), ("accent", Palette.accent)] {
            let c = Self.ratio(Self.luminance(Self.over(UIColor(fg).resolvedColor(with: traits), glass)), Self.luminance(glass))
            #expect(c >= 4.5, "\(name) on glass: \(c)")
        }
    }

    @Test func stageWordsAndLeaReadOnTheStage() {
        // Chalk captions and Lea's sky blue on the fixed stage and on the raised stage disc.
        for bg in [Palette.stage, Palette.stageRaised] {
            #expect(Self.contrast(Palette.chalk, bg, .light) >= 4.5)
            #expect(Self.contrast(Palette.next, bg, .light) >= 4.5)
            for tone in [Palette.cameraGood, Palette.cameraCaution, Palette.cameraDanger] {
                #expect(Self.contrast(tone, bg, .light) >= 4.5)
            }
        }
    }

    @Test func nextIsTheDarkModeAccent() {
        // One blue: the camera and stage `next` is the night accent, so Lea looks the same everywhere.
        let dark = UITraitCollection(userInterfaceStyle: .dark)
        #expect(Self.hex(UIColor(Palette.accent).resolvedColor(with: dark)) == FixedHex.next)
        #expect(Self.hex(UIColor(Palette.next)) == FixedHex.next)
    }

    /// The retheme (the owner, 2026-10-25): nothing violet or lavender is left, and the cosmetic and setup tones keep
    /// their distance from the blue accent.
    @Test(arguments: [UIUserInterfaceStyle.light, .dark])
    func noVioletAndTonesApartFromTheAccent(_ style: UIUserInterfaceStyle) {
        let traits = UITraitCollection(userInterfaceStyle: style)
        let tokens: [(String, Color)] = [
            ("canvas", Palette.canvas), ("surface", Palette.surface), ("well", Palette.well), ("ink", Palette.ink),
            ("secondary", Palette.secondary), ("accent", Palette.accent), ("onAccent", Palette.onAccent),
            ("rule", Palette.rule), ("hairline", Palette.hairline), ("glassFill", Palette.glassFill),
            ("glassEdge", Palette.glassEdge), ("cosmetic", Palette.cosmetic), ("setup", Palette.setup),
            ("next", Palette.next), ("stage", Palette.stage), ("stageRaised", Palette.stageRaised)]
        for (name, colour) in tokens {
            var (h, s, b, a): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
            UIColor(colour).resolvedColor(with: traits).getHue(&h, saturation: &s, brightness: &b, alpha: &a)
            // Violet and lavender: hue 245-320 degrees with any visible saturation.
            #expect(!(s > 0.04 && (245...320).contains(h * 360)), "\(name) is violet (hue \(Int(h * 360)), sat \(s))")
        }
        let accent = Self.hue(Palette.accent, traits)
        for (name, tone) in [("cosmetic", Palette.cosmetic), ("setup", Palette.setup)] {
            let d = abs(Self.hue(tone, traits) - accent)
            #expect(min(d, 360 - d) >= 40, "\(name) sits \(Int(min(d, 360 - d))) degrees from the accent")
        }
    }

    // MARK: Helpers

    static func contrast(_ a: Color, _ b: Color, _ style: UIUserInterfaceStyle) -> Double {
        let traits = UITraitCollection(userInterfaceStyle: style)
        let bg = UIColor(b).resolvedColor(with: traits)
        let fg = over(UIColor(a).resolvedColor(with: traits), bg)
        return ratio(luminance(fg), luminance(bg))
    }

    static func over(_ top: UIColor, _ bottom: UIColor) -> UIColor {
        var (r1, g1, b1, a1): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        var (r2, g2, b2, a2): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        top.getRed(&r1, green: &g1, blue: &b1, alpha: &a1)
        bottom.getRed(&r2, green: &g2, blue: &b2, alpha: &a2)
        return UIColor(red: r1 * a1 + r2 * (1 - a1), green: g1 * a1 + g2 * (1 - a1), blue: b1 * a1 + b2 * (1 - a1), alpha: 1)
    }

    static func luminance(_ c: UIColor) -> Double {
        var (r, g, b, a): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        c.getRed(&r, green: &g, blue: &b, alpha: &a)
        func lin(_ v: CGFloat) -> Double {
            let v = Double(v)
            return v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    }

    static func hue(_ c: Color, _ traits: UITraitCollection) -> Double {
        var (h, s, b, a): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        UIColor(c).resolvedColor(with: traits).getHue(&h, saturation: &s, brightness: &b, alpha: &a)
        return Double(h) * 360
    }

    static func hex(_ c: UIColor) -> UInt32 {
        var (r, g, b, a): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        c.getRed(&r, green: &g, blue: &b, alpha: &a)
        return (UInt32((r * 255).rounded()) << 16) | (UInt32((g * 255).rounded()) << 8) | UInt32((b * 255).rounded())
    }

    static func ratio(_ l1: Double, _ l2: Double) -> Double { (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05) }
}
