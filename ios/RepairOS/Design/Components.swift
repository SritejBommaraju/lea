import SwiftUI

// Shared building blocks for every screen. Press feedback is on touch-down (ButtonStyle.isPressed), springs come
// from Motion, colours from Palette/Tone. None of these use blur or shadows, so they are cheap anywhere.

// MARK: - Buttons

/// The one big action on a screen: a 56 pt capsule. `.accent` on app screens, `.light` (white, near-black label)
/// over the live camera, `.quiet` for the secondary action under a primary one.
struct PrimaryButtonStyle: ButtonStyle {
    enum Kind { case accent, light, quiet }
    var kind: Kind = .accent
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.buttonLabel)
            .labelStyle(.titleAndIcon)
            .foregroundStyle(foreground)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .padding(.horizontal, Spacing.xl)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(background, in: Capsule())
            .contentShape(Capsule())
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .opacity(isEnabled ? (configuration.isPressed ? 0.92 : 1) : 0.4)
            .animation(Motion.press, value: configuration.isPressed)
    }

    private var foreground: Color {
        switch kind {
        case .accent: .white
        case .light: Color(white: 0.06)
        case .quiet: Palette.accent
        }
    }

    private var background: Color {
        switch kind {
        case .accent: Palette.accentFill
        case .light: .white
        case .quiet: Palette.accent.opacity(0.12)
        }
    }
}

extension ButtonStyle where Self == PrimaryButtonStyle {
    static var primary: PrimaryButtonStyle { PrimaryButtonStyle() }
    static func primary(_ kind: PrimaryButtonStyle.Kind) -> PrimaryButtonStyle { PrimaryButtonStyle(kind: kind) }
}

/// A 44 pt round icon button (close, settings). `onCamera` uses a solid dark disc instead of a material.
struct RoundIconButtonStyle: ButtonStyle {
    var onCamera = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(onCamera ? Color.white : Color.primary)
            .frame(width: 44, height: 44)
            .background(onCamera ? Color.black.opacity(0.45) : Palette.surface, in: Circle())
            .overlay(Circle().strokeBorder(onCamera ? Color.white.opacity(0.12) : Palette.hairline, lineWidth: 0.5))
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// Makes a whole card pressable with the same touch-down feedback as the buttons.
struct PressableCardStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.9 : 1)
            .animation(Motion.press, value: configuration.isPressed)
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
                    if let tone { shape.fill(tone.color.opacity(0.07)) }
                }
                .overlay(shape.strokeBorder(tone.map { $0.color.opacity(0.22) } ?? Palette.hairline, lineWidth: 0.5))
            }
    }
}

extension View {
    /// A rounded surface on the canvas. `tone` washes it lightly (safety callouts, the next step).
    func card(padding: CGFloat = Spacing.l, tone: Tone? = nil) -> some View {
        modifier(CardModifier(padding: padding, tone: tone))
    }
}

/// A section heading on a scrolling screen: title, optional count, optional trailing accessory.
struct SectionHeader<Trailing: View>: View {
    let title: String
    var count: Int?
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Text(title).font(.sectionTitle)
            if let count {
                Text("\(count)").font(.pillLabel).monospacedDigit().foregroundStyle(.secondary)
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

// MARK: - Labels

/// An SF Symbol in a tinted rounded square, as in Settings. Scales with Dynamic Type.
struct IconBadge: View {
    let symbol: String
    var tone: Tone = .accent
    var filled = true
    @ScaledMetric(relativeTo: .body) private var size: CGFloat = 30

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size * 0.5, weight: .semibold))
            .foregroundStyle(filled ? Color.white : tone.color)
            .frame(width: size, height: size)
            .background(filled ? tone.color : tone.wash, in: RoundedRectangle(cornerRadius: size * 0.27, style: .continuous))
            .accessibilityHidden(true)
    }
}

/// A short status in a tinted capsule: icon + words. `onCamera` draws a solid dark capsule for use over AR.
struct StatusPill: View {
    let text: String
    var symbol: String?
    var tone: Tone = .neutral
    var onCamera = false

    var body: some View {
        HStack(spacing: 5) {
            if let symbol { Image(systemName: symbol).imageScale(.small) }
            Text(text).lineLimit(1)
        }
        .font(.pillLabel)
        .foregroundStyle(onCamera ? Color.white : (tone == .neutral ? Color.secondary : tone.color))
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(onCamera ? Color.black.opacity(0.5) : tone.wash, in: Capsule())
        .dynamicTypeSize(...(onCamera ? DynamicTypeSize.accessibility1 : .accessibility5))
        .accessibilityElement(children: .combine)
    }
}

/// A status pill with progress: a small ring when `fraction` is known, a spinner when it is not.
struct ProgressPill: View {
    let text: String
    var fraction: Double?
    var tone: Tone = .accent
    var onCamera = false

    var body: some View {
        HStack(spacing: 7) {
            if let fraction {
                ZStack {
                    Circle().stroke(ringColor.opacity(0.25), lineWidth: 2.5)
                    Circle().trim(from: 0, to: max(0.02, min(1, fraction)))
                        .stroke(ringColor, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 13, height: 13)
                .motion(Motion.standard, value: fraction)
            } else {
                ProgressView().controlSize(.mini).tint(ringColor)
            }
            Text(text).lineLimit(1).monospacedDigit()
        }
        .font(.pillLabel)
        .foregroundStyle(onCamera ? Color.white : tone.color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(onCamera ? Color.black.opacity(0.5) : tone.wash, in: Capsule())
        .dynamicTypeSize(...(onCamera ? DynamicTypeSize.accessibility1 : .accessibility5))
        .accessibilityElement(children: .combine)
        .accessibilityValue(fraction.map { "\(Int(($0 * 100).rounded())) percent" } ?? "in progress")
    }

    private var ringColor: Color { onCamera ? .white : tone.color }
}

/// A soft input row (symptom, gateway fields): icon + field in a well.
struct FieldWell<Field: View>: View {
    let symbol: String
    @ViewBuilder var field: Field

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: symbol).foregroundStyle(.secondary).accessibilityHidden(true)
            field.font(.body)
        }
        .padding(.horizontal, Spacing.l)
        .frame(minHeight: 52)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.medium, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
    }
}

// MARK: - Previews

#Preview("Components") {
    ScrollView {
        VStack(alignment: .leading, spacing: Spacing.l) {
            SectionHeader("Most likely problems", count: 3)
            HStack { StatusPill(text: "Safety", symbol: Symbol.safety, tone: .danger)
                StatusPill(text: "Medium confidence", tone: .neutral)
                ProgressPill(text: "12 of 30", fraction: 0.4) }
            HStack { IconBadge(symbol: Symbol.mac, tone: .accent); IconBadge(symbol: Symbol.privacy, tone: .good, filled: false) }
            Text("A card").card()
            Text("A safety card").card(tone: .danger)
            Button { } label: { Label("Start scan", systemImage: Symbol.scan) }.buttonStyle(.primary)
            Button("Not now") { }.buttonStyle(.primary(.quiet))
        }
        .padding(Spacing.gutter)
    }
    .background(Palette.canvas)
}
