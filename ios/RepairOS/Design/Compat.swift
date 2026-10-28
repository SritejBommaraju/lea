import AVFoundation
import Combine
import SwiftUI
import UIKit

// What the app uses from iOS 17 and 18 and how it still runs on iOS 16 (the minimum, see ios/README.md).
// Every helper here uses the newer API when the phone has it and a plain iOS 16 route when it does not, so a phone on
// iOS 17 or later behaves exactly as before. Nothing here asks which phone it is; only which OS APIs exist.

// MARK: - onChange (iOS 17 form with old and new value, and `initial`)

extension View {
    /// `onChange(of:initial:_:)` with the old and the new value. On iOS 16 it keeps the old value itself.
    @ViewBuilder
    func onValueChange<V: Equatable>(of value: V, initial: Bool = false, _ action: @escaping (V, V) -> Void) -> some View {
        if #available(iOS 17, *) {
            onChange(of: value, initial: initial, action)
        } else {
            modifier(LegacyValueChange(value: value, initial: initial, action: action))
        }
    }

    /// The same without the values.
    func onValueChange<V: Equatable>(of value: V, initial: Bool = false, _ action: @escaping () -> Void) -> some View {
        onValueChange(of: value, initial: initial) { _, _ in action() }
    }
}

private struct LegacyValueChange<V: Equatable>: ViewModifier {
    let value: V
    let initial: Bool
    let action: (V, V) -> Void
    @State private var previous: V?

    func body(content: Content) -> some View {
        content
            .onAppear {
                guard previous == nil else { return }
                previous = value
                if initial { action(value, value) }
            }
            .onChange(of: value) { new in
                action(previous ?? new, new)
                previous = new
            }
    }
}

// MARK: - Haptics (UIKit generators, the same on every iOS)

enum HapticKind: Equatable {
    case impact(UIImpactFeedbackGenerator.FeedbackStyle, CGFloat)
    case success, warning, selection
}

@MainActor
enum HapticPlayer {
    static func play(_ kind: HapticKind) {
        switch kind {
        case .impact(let style, let intensity): UIImpactFeedbackGenerator(style: style).impactOccurred(intensity: intensity)
        case .success: UINotificationFeedbackGenerator().notificationOccurred(.success)
        case .warning: UINotificationFeedbackGenerator().notificationOccurred(.warning)
        case .selection: UISelectionFeedbackGenerator().selectionChanged()
        }
    }
}

extension View {
    /// A haptic each time `trigger` changes.
    func haptic<T: Equatable>(_ kind: HapticKind, trigger: T) -> some View {
        onValueChange(of: trigger) { HapticPlayer.play(kind) }
    }

    /// A haptic when `trigger` changes and `when(old, new)` says so.
    func haptic<T: Equatable>(_ kind: HapticKind, trigger: T, when: @escaping (T, T) -> Bool) -> some View {
        onValueChange(of: trigger) { old, new in if when(old, new) { HapticPlayer.play(kind) } }
    }

    /// A haptic of the kind `kind(old, new)` returns, or none when it returns nil.
    func haptic<T: Equatable>(trigger: T, kind: @escaping (T, T) -> HapticKind?) -> some View {
        onValueChange(of: trigger) { old, new in if let k = kind(old, new) { HapticPlayer.play(k) } }
    }
}

// MARK: - Small modifiers that arrived after iOS 16

extension View {
    /// The symbol swaps with a replace effect (iOS 17); a plain swap before.
    @ViewBuilder func symbolReplaceTransition() -> some View {
        if #available(iOS 17, *) { contentTransition(.symbolEffect(.replace)) } else { self }
    }

    /// No bounce when the content fits (iOS 16.4).
    @ViewBuilder func bounceBasedOnSize() -> some View {
        if #available(iOS 16.4, *) { scrollBounceBehavior(.basedOnSize) } else { self }
    }

    /// Content may draw outside the scroll view's bounds (iOS 17); clipped before.
    @ViewBuilder func scrollUnclipped() -> some View {
        if #available(iOS 17, *) { scrollClipDisabled() } else { self }
    }

    /// A long list starts at its bottom (iOS 17); before, it scrolls to `id` (the newest line) once it has laid out.
    @ViewBuilder func startsAtBottom<ID: Hashable>(_ proxy: ScrollViewProxy, scrollingTo id: ID?) -> some View {
        if #available(iOS 17, *) {
            defaultScrollAnchor(.bottom)
        } else {
            onAppear {
                guard let id else { return }
                DispatchQueue.main.async { proxy.scrollTo(id, anchor: .bottom) }
            }
        }
    }

    /// A width computed from the screen's width (what `containerRelativeFrame` gave on iOS 17 for a full-width parent).
    @ViewBuilder func widthFromScreen(_ width: @escaping (CGFloat) -> CGFloat) -> some View {
        if #available(iOS 17, *) {
            containerRelativeFrame(.horizontal) { w, _ in width(w) }
        } else {
            frame(width: width(UIScreen.main.bounds.width))
        }
    }
}

/// A pinch (MagnifyGesture on iOS 17, MagnificationGesture before). `changed` gets the pinch's scale since it began.
struct PinchGestureModifier: ViewModifier {
    let changed: (CGFloat) -> Void
    let ended: () -> Void

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 17, *) {
            content.gesture(MagnifyGesture().onChanged { changed($0.magnification) }.onEnded { _ in ended() })
        } else {
            content.gesture(MagnificationGesture().onChanged { changed($0) }.onEnded { _ in ended() })
        }
    }
}

/// A rectangle with only its top corners rounded (UnevenRoundedRectangle is iOS 17).
struct TopRoundedShape: Shape {
    var radius: CGFloat

    func path(in rect: CGRect) -> Path {
        let r = max(0, min(radius, rect.width / 2, rect.height))
        var p = Path()
        p.move(to: CGPoint(x: rect.minX, y: rect.maxY))
        p.addLine(to: CGPoint(x: rect.minX, y: rect.minY + r))
        p.addArc(tangent1End: CGPoint(x: rect.minX, y: rect.minY), tangent2End: CGPoint(x: rect.minX + r, y: rect.minY), radius: r)
        p.addLine(to: CGPoint(x: rect.maxX - r, y: rect.minY))
        p.addArc(tangent1End: CGPoint(x: rect.maxX, y: rect.minY), tangent2End: CGPoint(x: rect.maxX, y: rect.minY + r), radius: r)
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        p.closeSubpath()
        return p
    }
}

// MARK: - Microphone permission (AVAudioApplication is iOS 17; AVAudioSession before)

enum MicPermission {
    enum State: String { case granted, denied, undetermined }

    static var state: State {
        if #available(iOS 17, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return .granted
            case .denied: return .denied
            default: return .undetermined
            }
        }
        switch AVAudioSession.sharedInstance().recordPermission {
        case .granted: return .granted
        case .denied: return .denied
        default: return .undetermined
        }
    }

    static func request() async -> Bool {
        if #available(iOS 17, *) { return await AVAudioApplication.requestRecordPermission() }
        return await withCheckedContinuation { c in
            AVAudioSession.sharedInstance().requestRecordPermission { c.resume(returning: $0) }
        }
    }
}

// MARK: - One-shot animations that used keyframes (iOS 17)

/// The check in `ShotTick`: pops in, holds, fades out each time `trigger` changes. Keyframes on iOS 17, springs before.
struct CheckPopModifier<T: Equatable>: ViewModifier {
    let trigger: T
    let reduceMotion: Bool
    @State private var scale: CGFloat = 1
    @State private var opacity: Double = 0
    @State private var fade: Task<Void, Never>?

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 17, *) {
            content.keyframeAnimator(initialValue: Pop(scale: reduceMotion ? 1 : 0.7, opacity: 0), trigger: trigger) { view, pop in
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
        } else {
            content.scaleEffect(scale).opacity(opacity)
                .onValueChange(of: trigger) {
                    scale = reduceMotion ? 1 : 0.7
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) { scale = 1; opacity = 1 }
                    fade?.cancel()   // a newer tick owns the fade
                    fade = Task { @MainActor in
                        try? await Task.sleep(for: .milliseconds(650))
                        guard !Task.isCancelled else { return }
                        withAnimation(.linear(duration: reduceMotion ? 0.08 : 0.22)) { opacity = 0; scale = reduceMotion ? 1 : 0.94 }
                    }
                }
        }
    }

    private struct Pop { var scale: CGFloat; var opacity: Double }
}

/// The Done button settles once (1, 1.04, 1) when `trigger` turns true. Keyframes on iOS 17, springs before.
struct SettleOnceModifier: ViewModifier {
    let trigger: Bool
    let reduceMotion: Bool
    @State private var scale: CGFloat = 1
    @State private var settle: Task<Void, Never>?

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 17, *) {
            content.keyframeAnimator(initialValue: CGFloat(1), trigger: trigger) { view, scale in
                view.scaleEffect(scale)
            } keyframes: { _ in
                KeyframeTrack {
                    if trigger && !reduceMotion {
                        SpringKeyframe(1.04, duration: 0.18, spring: .init(response: 0.4, dampingRatio: 0.78))
                        SpringKeyframe(1, duration: 0.3, spring: .init(response: 0.4, dampingRatio: 0.78))
                    } else {
                        LinearKeyframe(1, duration: 0.01)
                    }
                }
            }
        } else {
            content.scaleEffect(scale)
                .onValueChange(of: trigger) {
                    guard trigger, !reduceMotion else { return }
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.78)) { scale = 1.04 }
                    settle?.cancel()
                    settle = Task { @MainActor in
                        try? await Task.sleep(for: .milliseconds(180))
                        guard !Task.isCancelled else { return }
                        withAnimation(.spring(response: 0.4, dampingFraction: 0.78)) { scale = 1 }
                    }
                }
        }
    }
}

// MARK: - Watching an optional ObservableObject

/// `@ObservedObject` for an object that may be nil (the live events before the session is paired, a tour that is not
/// running): the view redraws when the object, if there is one, announces a change.
@MainActor @propertyWrapper
struct ObservedOptional<Object: ObservableObject>: DynamicProperty {
    @ObservedObject private var watcher: OptionalWatcher
    let wrappedValue: Object?

    init(wrappedValue: Object?) {
        self.wrappedValue = wrappedValue
        self.watcher = OptionalWatcher(wrappedValue)
    }
}

/// Forwards one object's changes as its own, so SwiftUI has something fixed to watch.
final class OptionalWatcher: ObservableObject {
    let objectWillChange = ObservableObjectPublisher()
    private var subscription: AnyCancellable?

    @MainActor
    init<O: ObservableObject>(_ object: O?) {
        let relay = objectWillChange
        subscription = object?.objectWillChange.sink { _ in relay.send() }
    }
}

// MARK: - Scroll position of the report stage (onScrollGeometryChange is iOS 18)

private enum StageScrollSpace { static let name = "lea.stage.scroll" }

private struct StageScrollKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

private struct StageHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

extension View {
    /// Put on the scroll view's content: before iOS 18 it measures how far the content has scrolled.
    @ViewBuilder func reportsScrollOffset() -> some View {
        if #available(iOS 18, *) {
            self
        } else {
            background(GeometryReader { g in
                Color.clear.preference(key: StageScrollKey.self, value: -g.frame(in: .named(StageScrollSpace.name)).minY)
            })
        }
    }

    /// Put on the scroll view: how far it is scrolled and how tall it is. Pair with `reportsScrollOffset()` on its content.
    @ViewBuilder func stageScrollMetrics(scrolled: Binding<CGFloat>, height: Binding<CGFloat>) -> some View {
        if #available(iOS 18, *) {
            onScrollGeometryChange(for: CGSize.self) {
                CGSize(width: $0.contentOffset.y + $0.contentInsets.top, height: $0.containerSize.height)
            } action: { _, g in
                scrolled.wrappedValue = g.width
                height.wrappedValue = g.height
            }
        } else {
            coordinateSpace(name: StageScrollSpace.name)
                .background(GeometryReader { g in Color.clear.preference(key: StageHeightKey.self, value: g.size.height) })
                .onPreferenceChange(StageScrollKey.self) { scrolled.wrappedValue = $0 }
                .onPreferenceChange(StageHeightKey.self) { if $0 > 0 { height.wrappedValue = $0 } }
        }
    }

    /// A height computed from the container's height (iOS 17); `fallback` before.
    @ViewBuilder func heightFromContainer(fallback: CGFloat, _ height: @escaping (CGFloat) -> CGFloat) -> some View {
        if #available(iOS 17, *) {
            containerRelativeFrame(.vertical) { h, _ in height(h) }
        } else {
            frame(height: fallback)
        }
    }
}

/// Redraws its content when an optional ObservableObject (read inside the content) announces a change.
struct Watching<O: ObservableObject, Content: View>: View {
    @ObservedOptional private var object: O?
    private let content: () -> Content

    init(_ object: O?, @ViewBuilder content: @escaping () -> Content) {
        _object = ObservedOptional(wrappedValue: object)
        self.content = content
    }

    var body: some View { content() }
}

// MARK: - Visibility, announcements and row groups

private struct OnScreenKey: PreferenceKey {
    static let defaultValue = true
    static func reduce(value: inout Bool, nextValue: () -> Bool) { value = nextValue() }
}

extension View {
    /// `visible` follows whether the view is on screen (scroll visibility is iOS 18; appear and disappear before).
    @ViewBuilder func tracksVisibility(_ visible: Binding<Bool>) -> some View {
        if #available(iOS 18, *) {
            onScrollVisibilityChange(threshold: 0.05) { visible.wrappedValue = $0 }
        } else {
            // Appear and disappear do not fire when a non-lazy stack scrolls a view off screen, so measure it.
            background(GeometryReader { g in
                Color.clear.preference(key: OnScreenKey.self, value: g.frame(in: .global).intersects(UIScreen.main.bounds))
            })
            .onPreferenceChange(OnScreenKey.self) { visible.wrappedValue = $0 }
        }
    }
}

enum Announcer {
    /// VoiceOver says `words` (AccessibilityNotification is iOS 17).
    @MainActor static func say(_ words: String) {
        if #available(iOS 17, *) {
            AccessibilityNotification.Announcement(words).post()
        } else {
            UIAccessibility.post(notification: .announcement, argument: words)
        }
    }
}

/// The rows of `content` with a hairline between them, each at least 44 pt (the pre-iOS 18 route to `Group(subviews:)`).
struct DividedRows: _VariadicView_MultiViewRoot {
    let inset: CGFloat

    @ViewBuilder
    func body(children: _VariadicView.Children) -> some View {
        ForEach(Array(children.enumerated()), id: \.offset) { i, row in
            if i > 0 { Rectangle().fill(Palette.hairline).frame(height: Line.hairline).padding(.leading, inset) }
            row.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .padding(.horizontal, inset)
        }
    }
}
