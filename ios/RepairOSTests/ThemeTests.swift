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

    static func ratio(_ l1: Double, _ l2: Double) -> Double { (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05) }
}
